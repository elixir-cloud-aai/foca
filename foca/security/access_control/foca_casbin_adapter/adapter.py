"""Casbin rule adapter."""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from bson import ObjectId, Timestamp
from casbin import persist
from casbin.model import Assertion, Model
from casbin.model.policy import DEFAULT_SEP
from pymongo import ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError, OperationFailure

from foca.security.access_control.constants import (
    CHANGES_COLLECTION_SUFFIX,
    CHANGES_EPOCH_FIELD,
    CHANGES_FIELD,
    CHANGES_MARKER_ID,
    CHANGES_VERSION_FIELD,
    CONFIGURED_FIELD,
    DEFAULT_ID_CHARSET,
    DEFAULT_ID_LENGTH,
    GENERATION_MARKER_ID,
    GRANTED_FIELD,
    ID_ATTEMPTS,
    LEGACY_GENERATION,
    LOAD_POLICY_ATTEMPTS,
    MARKER_GENERATION_FIELD,
    MAX_RECORDED_CHANGES,
    RULE_GENERATIONS_FIELD,
    STALE_GENERATION_AGE,
)
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    MAX_RULE_VALUES,
    CasbinRule,
    validate_rule,
)
from foca.utils.misc import generate_id

logger = logging.getLogger(__name__)

# Marks rules as granted now, by a timestamp of the database server, which
# is unique per server
_GRANTED_UPDATE = {"$currentDate": {GRANTED_FIELD: {"$type": "timestamp"}}}

# Indexes for queries of rules by subject (and role) or by resource
_FILTER_INDEXES = [[("ptype", 1), ("v0", 1), ("v1", 1)], [("ptype", 1), ("v2", 1)]]

# Recorded change of all rules (cf. `PolicyChanges`)
_ALL_CHANGED: dict[str, Any] = {"ptype": None}


@dataclass(frozen=True)
class PolicyChanges:
    """Changes of stored policy rules, as recorded by the adapter.

    Every write of the adapter records which stored rules it may have
    changed, so that cached enforcers (cf.
    :py:class:`foca.security.access_control.enforcer_cache.EnforcerCache`)
    of all app instances can apply the changes rather than load all rules
    again.

    Attributes:
        epoch: Identifier of the document recording the changes; it differs
            if the document was re-created (e.g., after the collection was
            dropped), so that versions are not compared across documents.
            `None` if no changes were recorded.
        version: Number of changes recorded since the document was created.
        changes: The latest changes, up to `MAX_RECORDED_CHANGES`, oldest
            first, the last one being change `version`; empty if only the
            version was read. Each change names the policy type
            (`ptype`), and the index (`index`) and values (`values`) of a
            filter matching the rules it may have changed, as for
            :py:meth:`Adapter.remove_filtered_policy`; if the policy type
            is `None`, all rules may have changed.
    """

    epoch: str | None
    version: int
    changes: tuple[dict[str, Any], ...] = ()


class Adapter(persist.Adapter):
    """Casbin adapter for MongoDB.

    Used for interactions between Casbin and MongoDB.

    Args:
        collection: MongoDB collection storing the policy rules.
        id_charset: Characters of generated rule identifiers.
        id_length: Length of generated rule identifiers.
        model: Path to the Casbin model file. If given, rules are validated
            against the model before they are stored (cf.
            :py:func:`validate_rule`), so that rules that would be skipped
            when loading policies are not stored in the first place.
        changes_collection: MongoDB collection recording the changes of
            stored rules (cf. :py:class:`PolicyChanges`). Defaults to the
            collection named as `collection` with suffix
            `CHANGES_COLLECTION_SUFFIX`, in the same database.

    Attributes:
        id_charset: Characters of generated rule identifiers.
        id_length: Length of generated rule identifiers.
        model: Casbin model to validate rules against, if any.
        changes_collection: MongoDB collection recording the changes of
            stored rules.

    """

    def __init__(
        self,
        collection: Collection[dict[str, Any]],
        id_charset: str = DEFAULT_ID_CHARSET,
        id_length: int = DEFAULT_ID_LENGTH,
        model: str | None = None,
        changes_collection: Collection[dict[str, Any]] | None = None,
    ):
        """Create an adapter for Mongodb."""
        self._collection = collection
        if changes_collection is None:
            changes_collection = collection.database[
                f"{collection.name}{CHANGES_COLLECTION_SUFFIX}"
            ]
        self.changes_collection = changes_collection
        self.id_charset = id_charset
        self.id_length = id_length
        self._id_index_created = False
        self._filter_indexes_created = False
        self.model: Model | None = None
        if model is not None:
            self.model = Model()
            self.model.load_model(model)

    def _ensure_id_index(self) -> None:
        """Ensure unique index on rule identifiers.

        The index is created on the first write rather than when creating the
        adapter, so that creating an adapter does not require a database
        connection. Rules without string identifiers (e.g., added to the
        database directly) are not indexed. If stored rules share
        identifiers (e.g., stored by previous versions), the index cannot be
        created; an error is logged once, and rules are stored without
        guaranteeing unique identifiers until the duplicates are resolved and
        the app is restarted.
        """
        if self._id_index_created:
            return
        try:
            # Idempotent, also for concurrent first writes
            self._collection.create_index(
                [("id", 1)],
                unique=True,
                partialFilterExpression={"id": {"$type": "string"}},
            )
        except DuplicateKeyError as exc:
            logger.error(
                "Unique index on policy rule identifiers could not be "
                "created, as rules in collection '%s' "
                "share identifiers; identifiers of new rules are not "
                "guaranteed to be unique. Delete or re-create the rules "
                "with duplicate identifiers and restart the app: %s",
                self._collection.name,
                exc,
            )
        # Other errors propagate, so that the next write tries again
        self._id_index_created = True

    def _ensure_filter_indexes(self) -> None:
        """Ensure indexes for queries of rules by values.

        Rules are queried by subject (`v0`) or resource (`v2`), e.g., when
        applying recorded changes (cf. :py:meth:`load_filtered_rules`), when
        revoking roles or when removing resources, so that these queries
        do not need to scan all rules. Created on the first recorded change,
        as for :py:meth:`_ensure_id_index`. If an index cannot be created
        (e.g., as an index with the same keys but other options exists), a
        warning is logged once, and rules are queried without it.
        """
        if self._filter_indexes_created:
            return
        for keys in _FILTER_INDEXES:
            try:
                # Idempotent, also for concurrent first writes
                self._collection.create_index(keys)
            # Other errors (e.g., connection errors) propagate, so that the
            # next write tries again
            except OperationFailure as exc:
                logger.warning(
                    "Index %s on policy rules in collection '%s' could not "
                    "be created; rules are queried without it: %s",
                    keys,
                    self._collection.name,
                    exc,
                )
        self._filter_indexes_created = True

    def load_policy(self, model: Model) -> None:
        """Implementing add Interface for casbin.

        Load all policy rules from MongoDB: the rules that are not
        configured, and the configured rules of the active generation (cf.
        :py:meth:`set_configured_policies`). If the active generation
        changes while rules are loaded, they are loaded again, so that the
        configured rules are those of one complete generation. Rules are
        built from the fields `ptype` and `v0` to `v5` of stored documents
        only. Invalid rules (cf. :py:func:`validate_rule`) are skipped with a
        warning. Rules stored more than once, or already in the model, are
        loaded once.

        Args:
            model: Casbin model to load rules into.

        Raises:
            RuntimeError: The active generation changed during each of
                `LOAD_POLICY_ATTEMPTS` attempts.
        """
        # Not `policy_map`: `clear_policy()` does not clear it on reloads
        loaded: dict[str, set[tuple[str, ...]]] = {}
        for ptype, values in self._load_rules({}, model):
            assertion = model[ptype[0]][ptype]
            if ptype not in loaded:
                loaded[ptype] = {tuple(rule) for rule in assertion.policy}
            key = tuple(values)
            if key in loaded[ptype]:
                continue
            loaded[ptype].add(key)
            _append_rule(assertion, values)

    def load_filtered_rules(
        self, model: Model, ptype: str, field_index: int, *field_values: str
    ) -> list[list[str]]:
        """Load the stored rules in effect that match a filter.

        Rules are loaded as by :py:meth:`load_policy`, but only those of
        policy type `ptype` with `field_values` starting at `field_index`,
        and they are returned rather than added to the model, e.g., to apply
        recorded changes (cf. :py:meth:`get_changes`) to a cached enforcer.

        Args:
            model: Casbin model to validate rules against.
            ptype: Policy type of the rules.
            field_index: Index of the value that `field_values` start at.
            field_values: Values the rules must have, starting at
                `field_index`.

        Returns:
            Values of the valid stored rules that match the filter, in the
            order they were stored, without duplicates.

        Raises:
            RuntimeError: The active generation changed during each of
                `LOAD_POLICY_ATTEMPTS` attempts.
        """
        query = _filter_query(ptype, field_index, field_values)
        rules = dict.fromkeys(
            tuple(values) for _, values in self._load_rules(query, model)
        )
        return [list(rule) for rule in rules]

    def _load_rules(
        self, query: dict[str, Any], model: Model
    ) -> list[tuple[str, list[str]]]:
        """Load the valid stored rules in effect that match a query.

        Args:
            query: Query for stored rules; the stored rules in effect that
                match it are loaded (cf. :py:meth:`load_policy`).
            model: Casbin model to validate rules against.

        Returns:
            Policy type and values of each valid rule, in the order they
            were stored, also if stored more than once.

        Raises:
            RuntimeError: The active generation changed during each of
                `LOAD_POLICY_ATTEMPTS` attempts.
        """
        for _ in range(LOAD_POLICY_ATTEMPTS):
            generation = self._active_generation()
            documents = list(
                self._collection.find({**query, **self._rules_query(generation)})
            )
            # Rules are only removed from generations that are no longer
            # active, and generations never become active again
            if self._active_generation() == generation:
                break
        else:
            raise RuntimeError(
                "Configured policy rules changed while loading rules, in "
                f"each of {LOAD_POLICY_ATTEMPTS} attempts"
            )
        rules = []
        for document in documents:
            ptype = document.get("ptype")
            values = _rule_values(document)
            try:
                validate_rule(ptype, values, model)
            except ValueError as exc:
                logger.warning(
                    "Skipping invalid policy rule '%s': %s", document.get("id"), exc
                )
                continue
            # Policy type is a string, as validated above
            rules.append((cast(str, ptype), values))
        return rules

    def get_changes(self, with_changes: bool = True) -> PolicyChanges:
        """Get the recorded changes of stored rules.

        Args:
            with_changes: Whether to also get the latest changes, rather than
                the version only (cf. :py:class:`PolicyChanges`).

        Returns:
            Recorded changes; version 0 without epoch if none were recorded.
        """
        projection = [CHANGES_EPOCH_FIELD, CHANGES_VERSION_FIELD]
        if with_changes:
            projection.append(CHANGES_FIELD)
        document = self.changes_collection.find_one(
            {"_id": CHANGES_MARKER_ID}, projection=projection
        )
        if document is None:
            return PolicyChanges(epoch=None, version=0)
        changes = document.get(CHANGES_FIELD, [])
        return PolicyChanges(
            epoch=str(document.get(CHANGES_EPOCH_FIELD)),
            version=document.get(CHANGES_VERSION_FIELD, 0),
            changes=tuple(changes) if isinstance(changes, list) else (),
        )

    def mark_changed(self) -> None:
        """Mark all stored rules as changed.

        Cached enforcers of all app instances then load all rules again
        before they are next used. Use this after changing stored rules
        other than via the adapter or FOCA's helpers and permissions API,
        e.g., directly in the database, which cached enforcers do not
        notice otherwise.
        """
        self._record_changes([_ALL_CHANGED])

    def _record_changes(self, changes: list[dict[str, Any]]) -> None:
        """Record changes of stored rules (cf. :py:class:`PolicyChanges`).

        Recorded after the rules were changed, so that enforcers reading
        the record find the changed rules; also if nothing was changed, so
        that retrying a write whose record failed records it.

        Args:
            changes: Changes to record, oldest first.
        """
        # Before enforcers query the changed rules
        self._ensure_filter_indexes()
        update = {
            "$inc": {CHANGES_VERSION_FIELD: len(changes)},
            "$push": {
                CHANGES_FIELD: {"$each": changes, "$slice": -MAX_RECORDED_CHANGES}
            },
            "$setOnInsert": {CHANGES_EPOCH_FIELD: str(ObjectId())},
        }
        # Again if the document was created concurrently, which then updates it
        for attempt in range(2):
            try:
                self.changes_collection.update_one(
                    {"_id": CHANGES_MARKER_ID}, update, upsert=True
                )
                return
            except DuplicateKeyError:
                if attempt:
                    raise

    def save_policy_line(
        self,
        ptype: str,
        rule: list[str],
        id: str | None = None,
        configured: bool = False,
    ) -> str:
        """Method to save a policy.

        Args:
            ptype: Policy type for the given rule based on the given conf file.
            rule: List of policy attributes.
            id: Identifier of the rule. If given, a stored rule with that
                identifier is replaced. Otherwise, a unique identifier is
                generated.
            configured: Whether to store the rule as set via the app's
                configuration (`access_control.policies`), in the active
                generation (cf. :py:meth:`set_configured_policies`); it is
                then removed when configured rules are next replaced. Not
                safe while configured rules are replaced concurrently, as
                the rule may then join a generation that is no longer
                active.

        Returns:
            Identifier of the stored rule.

        Raises:
            ValueError: The rule is invalid (cf. :py:func:`validate_rule`),
                also for the adapter's model, if set.
            pymongo.errors.DuplicateKeyError: No unique identifier could be
                generated in `ID_ATTEMPTS` attempts.
        """
        validate_rule(ptype, rule, self.model)
        self._ensure_id_index()
        document = CasbinRule.from_values(ptype, rule).to_dict()
        if configured:
            document[CONFIGURED_FIELD] = True
            document[RULE_GENERATIONS_FIELD] = [self._active_generation()]
        changes = [_change(ptype, 0, rule)]
        if id is not None:
            document["id"] = id
            # Configured rules are only replaced by configured rules
            query: dict[str, Any] = {"id": id}
            if not configured:
                query[CONFIGURED_FIELD] = {"$ne": True}
            # Recorded before replacing, too, as a retry would not find the
            # replaced rule anymore (cf. `remove_policy_by_id()`)
            current_change = _document_change(self._collection.find_one(query))
            if current_change is not None:
                self._record_changes([current_change, *changes])
            replaced = self._collection.find_one_and_replace(
                query, document, upsert=True
            )
            for change in (_document_change(replaced), current_change):
                if change is not None and change not in changes:
                    changes.insert(0, change)
        else:
            id = self._insert(document)
        self._record_changes(changes)
        return id

    def _insert(self, document: dict[str, Any]) -> str:
        """Store a rule with a generated identifier.

        Args:
            document: Rule to store, without identifier.

        Returns:
            Identifier of the stored rule.

        Raises:
            pymongo.errors.DuplicateKeyError: No unique identifier could be
                generated in `ID_ATTEMPTS` attempts.
        """
        attempts_left = ID_ATTEMPTS
        while True:
            document["id"] = generate_id(charset=self.id_charset, length=self.id_length)
            try:
                self._collection.insert_one(document)
                return document["id"]
            except DuplicateKeyError:
                attempts_left -= 1
                if not attempts_left:
                    raise
                logger.debug("Generated rule identifier exists; retrying")
                document.pop("_id", None)

    def replace_role_rule(
        self, ptype: str, rule: list[str], roles: Iterable[str]
    ) -> bool:
        """Store a role rule, replacing other roles of its subject.

        The rule is stored first (or, if stored before, marked as granted
        anew), then the subject's other rules with one of `roles` on the
        resource are removed, so that the subject keeps a role throughout.
        Rules are ordered by when they were stored or marked, via a
        timestamp of the database server (`granted`), and only the latest
        one is kept, so that concurrent replacements for the same subject
        and resource leave one role, that of the latest call; rules stored
        before without timestamp count as the oldest. If removing the other
        roles fails, a rule stored by this call is removed again, so that
        the subject is left with fewer rights, not more.

        Rules marked as set via the app's configuration
        (`access_control.policies`) are neither replaced nor marked, as they
        would be added again on the next start; if the rule is only stored
        with that mark, an unmarked copy is stored.

        Args:
            ptype: Policy type of the rule.
            rule: Values of the rule: subject, role and resource.
            roles: Roles to replace, besides the rule's role.

        Returns:
            `True` if the rule was not stored before, `False` if it was
            (also if only with the configured mark).

        Raises:
            ValueError: The rule does not have three values, or is invalid
                (cf. :py:func:`validate_rule`).
            pymongo.errors.DuplicateKeyError: No unique identifier could be
                generated in `ID_ATTEMPTS` attempts.
        """
        if len(rule) != 3:
            raise ValueError("Role rules have three values: subject, role, resource")
        validate_rule(ptype, rule, self.model)
        roles = list(roles)
        subject, role, resource = rule
        self._ensure_id_index()
        try:
            marked = self._collection.update_many(
                {**_rule_query(ptype, rule), CONFIGURED_FIELD: {"$ne": True}},
                _GRANTED_UPDATE,
            ).matched_count
            inserted = None if marked else self._insert_granted(ptype, rule)
            try:
                # Before the other roles are removed, so that cached
                # enforcers see the subject with both roles meanwhile, as
                # when loading all rules
                if inserted is not None:
                    self._record_changes([_change(ptype, 0, rule)])
                self._keep_latest_role(ptype, rule, roles)
            # Also if interrupted, so that the subject is not left with more
            # rights
            except BaseException:
                # Unless granted again concurrently, by a call that succeeds
                if inserted is not None:
                    self._remove_inserted(ptype, rule, inserted)
                raise
        # Also if failed, as rules may have been changed
        finally:
            self._record_changes(
                [
                    _change(ptype, 0, [subject, other, resource])
                    for other in dict.fromkeys([role, *roles])
                ]
            )
        # A configured copy grants the role already, also without our copy
        return inserted is not None and not self.is_configured_rule(ptype, rule)

    def _remove_inserted(
        self, ptype: str, rule: list[str], inserted: dict[str, Any]
    ) -> None:
        """Remove a rule stored by a failed replacement, unless granted again.

        If removing it fails, too, an error is logged, so that the rule can
        be revoked once the database is available, and the error of the
        replacement is raised by the caller.

        Args:
            ptype: Policy type of the rule.
            rule: Values of the rule.
            inserted: Identifier and timestamp of the stored rule (cf.
                :py:meth:`_insert_granted`).
        """
        try:
            self._collection.delete_one(inserted)
        except Exception as exc:
            logger.error(
                "Role rule '%s' could not be removed after replacing other "
                "roles failed, so the subject may have more than one role; "
                "revoke the role or grant it again: %s",
                CasbinRule.from_values(ptype, rule),
                exc,
            )

    def _insert_granted(self, ptype: str, rule: list[str]) -> dict[str, Any]:
        """Store a rule with the time it was granted.

        Args:
            ptype: Policy type of the rule.
            rule: Values of the rule.

        Returns:
            MongoDB identifier (`_id`) and timestamp (`granted`) of the
            stored rule, as a query matching it until it is granted again.

        Raises:
            pymongo.errors.DuplicateKeyError: No unique identifier could be
                generated in `ID_ATTEMPTS` attempts.
        """
        document = CasbinRule.from_values(ptype, rule).to_dict()
        attempts_left = ID_ATTEMPTS
        while True:
            id = generate_id(charset=self.id_charset, length=self.id_length)
            # An upsert, as the timestamp is set by the database server; the
            # new MongoDB identifier matches no stored rule, so that the rule
            # is always inserted
            try:
                return cast(
                    dict[str, Any],
                    self._collection.find_one_and_update(
                        {**document, "id": id, "_id": ObjectId()},
                        _GRANTED_UPDATE,
                        projection=[GRANTED_FIELD],
                        upsert=True,
                        return_document=ReturnDocument.AFTER,
                    ),
                )
            except DuplicateKeyError:
                attempts_left -= 1
                if not attempts_left:
                    raise
                logger.debug("Generated rule identifier exists; retrying")

    def _keep_latest_role(
        self, ptype: str, rule: list[str], roles: Iterable[str]
    ) -> None:
        """Remove role rules of a subject on a resource but the latest.

        Rules are only removed if they were not changed since they were
        read, so that a rule granted again concurrently is kept (and the
        older rules are removed by the call granting it), as are rules
        changed or marked as configured meanwhile.

        Args:
            ptype: Policy type of the rules.
            rule: Values of the granted rule: subject, role and resource.
            roles: Other roles of the subject on the resource to remove.
        """
        subject, role, resource = rule
        query: dict[str, Any] = {
            "ptype": ptype,
            "v0": subject,
            "v1": {"$in": [role, *roles]},
            "v2": resource,
            CONFIGURED_FIELD: {"$ne": True},
        }
        for index in range(3, MAX_RULE_VALUES):
            query[f"v{index}"] = None
        documents = list(self._collection.find(query, projection=[GRANTED_FIELD, "v1"]))
        if len(documents) < 2:
            return
        latest = max(documents, key=_granted_order)
        self._collection.delete_many(
            {
                "$or": [
                    {
                        **_rule_query(ptype, [subject, document["v1"], resource]),
                        "_id": document["_id"],
                        GRANTED_FIELD: document.get(GRANTED_FIELD, {"$exists": False}),
                        CONFIGURED_FIELD: {"$ne": True},
                    }
                    for document in documents
                    if document["_id"] != latest["_id"]
                ]
            }
        )

    def _delete_policy_lines(self, ptype: str, rule: list[str]) -> int:
        """Delete stored rules with exactly the given values.

        Rules marked as set via the app's configuration
        (`access_control.policies`) are not deleted, as they would be added
        again on the next start; checking the mark is part of the delete,
        so that rules marked concurrently are not deleted either.

        Args:
            ptype: Policy type for the given rule based on the given conf file.
            rule: List of policy attributes.

        Returns:
            Count of policies deleted.
        """
        if not rule:
            return 0
        query = {**_rule_query(ptype, rule), CONFIGURED_FIELD: {"$ne": True}}
        deleted = self._collection.delete_many(query).deleted_count
        self._record_changes([_change(ptype, 0, rule)])
        return deleted

    def remove_policy_by_id(self, id: str) -> bool:
        """Remove a stored rule by its identifier.

        Rules marked as set via the app's configuration
        (`access_control.policies`) are not removed (cf.
        :py:meth:`_delete_policy_lines`).

        Args:
            id: Identifier of the rule.

        The removal is recorded before and after removing the rule: a retry
        would not find the rule anymore to record its removal, e.g., after
        recording failed or the app instance stopped meanwhile.

        Returns:
            Whether a rule was removed.
        """
        query = {"id": id, CONFIGURED_FIELD: {"$ne": True}}
        current = self._collection.find_one(query)
        if current is None:
            return False
        changes = [change for change in [_document_change(current)] if change]
        if changes:
            self._record_changes(changes)
        removed = self._collection.find_one_and_delete(query)
        if removed is None:
            return False
        removed_change = _document_change(removed)
        if removed_change is not None and removed_change not in changes:
            changes.append(removed_change)
        if changes:
            self._record_changes(changes)
        return True

    def is_configured(self, id: str) -> bool:
        """Check whether a stored rule is set via the app's configuration.

        Args:
            id: Identifier of the rule.

        Returns:
            Whether a rule with identifier `id` is stored as a configured rule
            of the active generation (cf. :py:meth:`set_configured_policies`).
        """
        return self._has_configured({"id": id})

    def is_configured_rule(self, ptype: str, rule: list[str]) -> bool:
        """Check whether a stored rule with given values is configured.

        Args:
            ptype: Policy type of the rule.
            rule: Values of the rule.

        Returns:
            Whether a rule with exactly these values is stored as a
            configured rule of the active generation (cf.
            :py:meth:`set_configured_policies`).
        """
        return self._has_configured(_rule_query(ptype, rule))

    def has_configured_policies(self) -> bool:
        """Check whether configured rules are stored.

        Returns:
            Whether configured rules of any generation (cf.
            :py:meth:`set_configured_policies`) are stored, e.g., also of
            generations that never became active.
        """
        query = {CONFIGURED_FIELD: True}
        return self._collection.count_documents(query, limit=1) > 0

    def get_filtered_rules(
        self,
        ptype: str,
        field_index: int,
        *field_values: str,
        configured: bool,
    ) -> list[list[str]]:
        """Get stored rules that match a filter, by configured mark.

        Args:
            ptype: Policy type of the rules.
            field_index: Index of the value that `field_values` start at.
            field_values: Values the rules must have, starting at
                `field_index`.
            configured: Whether to get the configured rules of the active
                generation (cf. :py:meth:`set_configured_policies`) or the
                rules that are not configured.

        Returns:
            Values of the stored rules that match the filter and the mark,
            without duplicates.
        """
        query = _filter_query(ptype, field_index, field_values)
        if configured:
            query.update(self._configured_query(self._active_generation()))
        else:
            query[CONFIGURED_FIELD] = {"$ne": True}
        rules = dict.fromkeys(
            tuple(_rule_values(document)) for document in self._collection.find(query)
        )
        return [list(rule) for rule in rules]

    def _has_configured(self, query: dict[str, Any]) -> bool:
        """Check whether a stored rule matching a query is configured.

        Args:
            query: Query for stored rules.

        Returns:
            Whether a stored rule matches `query` and is a configured rule of
            the active generation.
        """
        query = {**query, **self._configured_query(self._active_generation())}
        return self._collection.count_documents(query, limit=1) > 0

    def _active_generation(self) -> str:
        """Get the active generation of configured rules.

        Returns:
            Generation named by the marker document, or `LEGACY_GENERATION`
            if there is none (i.e., configured rules were last set by FOCA
            0.16 or earlier, if at all).
        """
        marker = self._collection.find_one(
            {"_id": GENERATION_MARKER_ID}, projection=[MARKER_GENERATION_FIELD]
        )
        if marker is None:
            return LEGACY_GENERATION
        return str(marker.get(MARKER_GENERATION_FIELD))

    @staticmethod
    def _configured_query(generation: str) -> dict[str, Any]:
        """Get query matching the configured rules of a generation.

        Args:
            generation: Generation of configured rules.

        Returns:
            Query matching configured rules of `generation`; for
            `LEGACY_GENERATION`, also configured rules without generations.
        """
        if generation == LEGACY_GENERATION:
            # Also matches rules without the field
            return {
                CONFIGURED_FIELD: True,
                RULE_GENERATIONS_FIELD: {"$in": [generation, None]},
            }
        return {CONFIGURED_FIELD: True, RULE_GENERATIONS_FIELD: generation}

    def _rules_query(self, generation: str) -> dict[str, Any]:
        """Get query matching the rules in effect for a generation.

        Args:
            generation: Active generation of configured rules.

        Returns:
            Query matching the stored rules that are not configured and the
            configured rules of `generation`, but not the marker document.
        """
        return {
            "_id": {"$ne": GENERATION_MARKER_ID},
            "$or": [
                {CONFIGURED_FIELD: {"$ne": True}},
                self._configured_query(generation),
            ],
        }

    def rules_query(self) -> dict[str, Any]:
        """Get query matching the stored rules in effect.

        Returns:
            Query matching the stored rules that are not configured and the
            configured rules of the active generation (cf.
            :py:meth:`set_configured_policies`), as loaded for enforcing
            policies.
        """
        return self._rules_query(self._active_generation())

    def set_configured_policies(self, rules: list[list[str]]) -> None:
        """Replace the stored configured rules with the given rules.

        Rules are replaced without transactions, but such that rules are
        always loaded with one complete set of configured rules: the given
        rules are stored as a new generation, which is then made the active
        one, in a single write to the marker document; finally, the
        configured rules of the previously active generation that are not
        given are removed. Stored configured rules with the values of a
        given rule are added to the new generation rather than stored again,
        so that their identifiers are kept; duplicates (e.g., added by app
        instances starting concurrently) are thus removed. Generations that
        never became active (e.g., because the app instance storing them
        failed) are removed once they are older than
        `STALE_GENERATION_AGE`.

        If app instances replace configured rules concurrently, the rules of
        the last one to make its generation active are kept. Configured rules
        stored by FOCA 0.16 and earlier, without generations, belong to
        generation `LEGACY_GENERATION`, which is active until another one is.
        Stored rules that are not configured (e.g., added via the
        permissions API), are kept, also if they have the values of a given
        rule.

        Args:
            rules: Configured rules, each given as its policy type followed
                by its values; they need to be valid (cf.
                :py:func:`validate_rule`).
        """
        self._ensure_id_index()
        generation = str(ObjectId())
        self._store_generation(generation, rules)
        try:
            previous = self._activate_generation(generation)
        # The generation may have been activated nonetheless
        except BaseException:
            self._record_changes([_ALL_CHANGED])
            raise
        try:
            self._record_configured_changes(previous, rules)
        # Rules of the previous generation are no longer read afterwards
        except BaseException:
            self._record_changes([_ALL_CHANGED])
            raise
        self._remove_generations([previous])
        self._remove_stale_generations()

    def _record_configured_changes(self, previous: str, rules: list[list[str]]) -> None:
        """Record the changes of replacing the active configured rules.

        Only the rules that are configured in one of the generations but not
        in the other are recorded, so that cached enforcers need not load
        all rules again, e.g., whenever an app instance starts.

        Args:
            previous: Previously active generation, whose rules are not
                removed yet.
            rules: Configured rules of the active generation, each given as
                its policy type followed by its values.
        """
        old = {
            (document.get("ptype"), *_rule_values(document))
            for document in self._collection.find(self._configured_query(previous))
        }
        changes = [
            change
            for ptype, *values in old ^ {tuple(rule) for rule in rules}
            if (change := _rule_change(ptype, values)) is not None
        ]
        if len(changes) > MAX_RECORDED_CHANGES:
            changes = [_ALL_CHANGED]
        if changes:
            self._record_changes(changes)

    def _store_generation(self, generation: str, rules: list[list[str]]) -> None:
        """Store configured rules as a generation that is not active yet.

        Configured rules without generations (stored by FOCA 0.16 and
        earlier) are first added to `LEGACY_GENERATION`. Each given rule is
        then added to `generation`: a stored configured copy of the rule is
        added to it, preferably the one of the active generation, so that
        identifiers of rules in effect are kept; otherwise, the rule is
        stored.

        Args:
            generation: New generation.
            rules: Configured rules, each given as its policy type followed
                by its values.
        """
        self._collection.update_many(
            {CONFIGURED_FIELD: True, RULE_GENERATIONS_FIELD: {"$exists": False}},
            {"$set": {RULE_GENERATIONS_FIELD: [LEGACY_GENERATION]}},
        )
        active = self._active_generation()
        add = {"$addToSet": {RULE_GENERATIONS_FIELD: generation}}
        for ptype, *rule in dict.fromkeys(tuple(rule) for rule in rules):
            query = _rule_query(ptype, rule)
            if self._collection.update_one(
                {**query, **self._configured_query(active)}, add
            ).matched_count:
                continue
            if self._collection.update_one(
                {
                    **query,
                    CONFIGURED_FIELD: True,
                    RULE_GENERATIONS_FIELD: {"$exists": True},
                },
                add,
            ).matched_count:
                continue
            document = CasbinRule.from_values(ptype, rule).to_dict()
            document[CONFIGURED_FIELD] = True
            document[RULE_GENERATIONS_FIELD] = [generation]
            id = self._insert(document)
            logger.info(
                "Stored configured policy rule '%s' (generation '%s')", id, generation
            )

    def _activate_generation(self, generation: str) -> str:
        """Make a generation of configured rules the active one.

        Args:
            generation: Generation to activate.

        Returns:
            Previously active generation.
        """
        # One write, so that the rules of one generation are in effect;
        # again if the marker was created concurrently, which then updates it
        for attempt in range(2):
            try:
                previous = self._collection.find_one_and_update(
                    {"_id": GENERATION_MARKER_ID},
                    {"$set": {MARKER_GENERATION_FIELD: generation}},
                    upsert=True,
                    return_document=ReturnDocument.BEFORE,
                )
                break
            except DuplicateKeyError:
                if attempt:
                    raise
        logger.info("Configured policy rules replaced (generation '%s')", generation)
        if previous is None:
            return LEGACY_GENERATION
        return str(previous.get(MARKER_GENERATION_FIELD))

    def _remove_generations(self, generations: list[str]) -> None:
        """Remove generations of configured rules.

        Configured rules that are then in no generation are removed.

        Args:
            generations: Generations to remove; they must not be active.
        """
        self._collection.update_many(
            {CONFIGURED_FIELD: True, RULE_GENERATIONS_FIELD: {"$in": generations}},
            {"$pull": {RULE_GENERATIONS_FIELD: {"$in": generations}}},
        )
        unused = {CONFIGURED_FIELD: True, RULE_GENERATIONS_FIELD: {"$size": 0}}
        for document in self._collection.find(unused, projection=["id"]):
            # Unless added to a generation meanwhile
            if self._collection.delete_one(
                {**unused, "_id": document["_id"]}
            ).deleted_count:
                logger.info("Removed configured policy rule '%s'", document.get("id"))

    def _remove_stale_generations(self) -> None:
        """Remove generations of configured rules that never became active.

        Generations other than the active one are removed if they are
        `LEGACY_GENERATION`, older than `STALE_GENERATION_AGE`, or not
        generations created by :py:meth:`set_configured_policies`. Newer
        generations are kept, as their rules may still be being stored.
        """
        active = self._active_generation()
        created_before = datetime.now(UTC) - STALE_GENERATION_AGE
        stale = [
            generation
            for generation in self._collection.distinct(
                RULE_GENERATIONS_FIELD, {CONFIGURED_FIELD: True}
            )
            if generation != active
            and (
                not isinstance(generation, str)
                or not ObjectId.is_valid(generation)
                or ObjectId(generation).generation_time < created_before
            )
        ]
        if stale:
            self._remove_generations(stale)

    def save_policy(self, model: Model) -> bool:
        """Method to save a casbin model.

        Replaces all stored rules with the rules of the model, except for
        configured rules (cf. :py:meth:`set_configured_policies`), which are
        kept and not stored again, so that they stay managed via the
        configuration.

        Args:
            model: Casbin Model which loads from .conf file. For model
                description, cf. https://github.com/casbin/pycasbin/blob/72571\
                5fc04b3f37f26eb4be1ba7007ddf55d1e75/casbin/model/model.py#L23

        Returns:
            True if successfully created.
        """
        try:
            self._collection.delete_many(
                {"_id": {"$ne": GENERATION_MARKER_ID}, CONFIGURED_FIELD: {"$ne": True}}
            )
            for section_type in ["p", "g"]:
                for ptype, ast in model.model[section_type].items():
                    for rule in ast.policy:
                        if not self.is_configured_rule(ptype, rule):
                            self._save_rule(ptype, rule)
        # Also if failed, as rules may have been removed
        finally:
            self._record_changes([_ALL_CHANGED])
        return True

    def _save_rule(self, ptype: str, rule: list[str]) -> None:
        """Store a rule, without recording the change.

        Args:
            ptype: Policy type of the rule.
            rule: Values of the rule; they need to be valid (cf.
                :py:func:`validate_rule`).
        """
        validate_rule(ptype, rule, self.model)
        self._ensure_id_index()
        self._insert(CasbinRule.from_values(ptype, rule).to_dict())

    def add_policy(self, sec: str, ptype: str, rule: list[str]) -> bool:
        """Add policy rules to MongoDB.

        Args:
            sec: Section corresponding which the rule will be added.
            ptype: Policy type for which casbin rule will be added.
            rule: Casbin rule list definition to be added.

        Returns:
            True if succeed else False.
        """
        self.save_policy_line(ptype, rule)
        return True

    def remove_policy(self, sec: str, ptype: str, rule: list[str]) -> bool:
        """Remove policy rules from mongodb(duplicate rules are also removed).

        Copies marked as set via the app's configuration are kept (cf.
        :py:meth:`_delete_policy_lines`).

        Args:
            sec: Section corresponding which the rule will be added.
            ptype: Policy type for which casbin rule will be removed.
            rule: Casbin rule list definition to be removed.

        Returns:
            Whether any copies of the rule were removed.
        """
        deleted_count = self._delete_policy_lines(ptype, rule)
        return deleted_count > 0

    def remove_filtered_policy(
        self,
        sec: str,
        ptype: str,
        field_index: int,
        *field_values: str,
        keep_configured: bool = False,
    ) -> bool:
        """Remove policy rules that match the filter from the storage.

        This is part of the Auto-Save feature.

        Args:
            sec: Section corresponding which the rule will be added.
            ptype: Policy type for which casbin rule will be removed.
            field_index: The policy index at which the field_values begin
                filtering. Its range is [0, 5]
            field_values: A list of rules to filter policy.
            keep_configured: Whether to keep rules marked as set via the
                app's configuration (`access_control.policies`).

        Returns:
           True if success.
        """
        if not (0 <= field_index <= 5):
            return False
        if not (1 <= field_index + len(field_values) <= 6):
            return False

        query = _filter_query(ptype, field_index, field_values)
        if keep_configured:
            query[CONFIGURED_FIELD] = {"$ne": True}
        results = self._collection.delete_many(query)
        # Enforcers remove rules matching empty values (as wildcards) in
        # memory, unlike stored rules: they need to load all rules again
        if "" in field_values:
            self._record_changes([_ALL_CHANGED])
        else:
            self._record_changes([_change(ptype, field_index, field_values)])
        return results.deleted_count > 0


def _change(ptype: str, field_index: int, values: Sequence[str]) -> dict[str, Any]:
    """Get a recorded change of the rules matching a filter.

    Args:
        ptype: Policy type of the rules.
        field_index: Index of the value that `values` start at.
        values: Values the rules have, starting at `field_index`.

    Returns:
        Change, as recorded (cf. :py:class:`PolicyChanges`).
    """
    return {"ptype": ptype, "index": field_index, "values": list(values)}


def _rule_change(ptype: Any, values: Sequence[Any]) -> dict[str, Any] | None:
    """Get a recorded change of a stored rule, if it may have been loaded.

    Args:
        ptype: Policy type of the rule.
        values: Values of the rule.

    Returns:
        Change of the rules with the values (cf. :py:func:`_change`), or
        `None` if the policy type or a value is not a string, so that the
        rule was not loaded.
    """
    if not isinstance(ptype, str) or not all(isinstance(v, str) for v in values):
        return None
    return _change(ptype, 0, values)


def _document_change(document: dict[str, Any] | None) -> dict[str, Any] | None:
    """Get a recorded change of a stored rule (cf. :py:func:`_rule_change`).

    Args:
        document: Stored rule, if any.

    Returns:
        Change of the rule, or `None` if there is none, or if it was not
        loaded.
    """
    if document is None:
        return None
    return _rule_change(document.get("ptype"), _rule_values(document))


def _granted_order(document: dict[str, Any]) -> tuple[bool, Timestamp, str]:
    """Get the order of a stored rule by when it was granted.

    Args:
        document: Stored rule.

    Returns:
        Sort key: rules without timestamp (e.g., stored by
        :py:meth:`Adapter.save_policy_line`) come first, ties are broken by
        MongoDB identifier.
    """
    granted = document.get(GRANTED_FIELD)
    if not isinstance(granted, Timestamp):
        return (False, Timestamp(0, 0), str(document["_id"]))
    return (True, granted, str(document["_id"]))


def _append_rule(assertion: Assertion, values: list[str]) -> None:
    """Append a rule to a policy of a Casbin model.

    Args:
        assertion: Policy definition of the model to append the rule to.
        values: Values of the rule.
    """
    # Mirrors casbin's `persist.load_policy_line()` and `Policy.add_policy()`
    # without the latter's duplicate scan, which makes loading quadratic.
    # `Enforcer.load_policy()` re-sorts the rules after loading (for priority
    # models), rebuilding `policy_map`, which pycasbin 2.8.0 never reads.
    # pycasbin is capped at `<2.9` in `pyproject.toml`; re-check this when
    # raising the cap.
    assertion.policy.append(values)
    assertion.policy_map[DEFAULT_SEP.join(values)] = len(assertion.policy) - 1


def _rule_query(ptype: str, rule: list[str]) -> dict[str, Any]:
    """Get query matching stored rules with exactly the given values.

    Args:
        ptype: Policy type of the rule.
        rule: Values of the rule.

    Returns:
        Query matching the rule's values exactly, i.e., without further
        values, but irrespective of other stored fields (e.g., identifiers
        or the configured mark).
    """
    query: dict[str, Any] = CasbinRule.from_values(ptype, rule).to_dict()
    # Match fields that are not set or null, as when loading rules
    for index in range(len(rule), MAX_RULE_VALUES):
        query[f"v{index}"] = None
    return query


def _filter_query(
    ptype: str, field_index: int, field_values: tuple[str, ...]
) -> dict[str, Any]:
    """Get query matching stored rules with the given values.

    Args:
        ptype: Policy type of the rules.
        field_index: Index of the value that `field_values` start at.
        field_values: Values the rules must have, starting at `field_index`.

    Returns:
        Query matching rules of policy type `ptype` with `field_values`,
        irrespective of their other values and stored fields.
    """
    query: dict[str, Any] = {"ptype": ptype}
    for index, value in enumerate(field_values):
        query[f"v{index + field_index}"] = value
    return query


def _rule_values(document: dict[str, Any]) -> list[Any]:
    """Get values of a stored rule.

    Args:
        document: Stored rule.

    Returns:
        Values of fields `v0` to `v5`, without trailing values that are not
        set or null.
    """
    values = [document.get(f"v{index}") for index in range(MAX_RULE_VALUES)]
    while values and values[-1] is None:
        values.pop()
    return values
