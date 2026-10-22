"""Casbin rule adapter."""

import logging
from collections.abc import Iterable
from typing import Any, cast

from bson import ObjectId, Timestamp
from casbin import persist
from casbin.model import Assertion, Model
from casbin.model.policy import DEFAULT_SEP
from pymongo import ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from foca.security.access_control.constants import (
    CONFIGURED_FIELD,
    DEFAULT_ID_CHARSET,
    DEFAULT_ID_LENGTH,
    GRANTED_FIELD,
    ID_ATTEMPTS,
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

    Attributes:
        id_charset: Characters of generated rule identifiers.
        id_length: Length of generated rule identifiers.
        model: Casbin model to validate rules against, if any.

    """

    def __init__(
        self,
        collection: Collection[dict[str, Any]],
        id_charset: str = DEFAULT_ID_CHARSET,
        id_length: int = DEFAULT_ID_LENGTH,
        model: str | None = None,
    ):
        """Create an adapter for Mongodb."""
        self._collection = collection
        self.id_charset = id_charset
        self.id_length = id_length
        self._id_index_created = False
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

    def load_policy(self, model: Model) -> None:
        """Implementing add Interface for casbin.

        Load all policy rules from MongoDB. Rules are built from the fields
        `ptype` and `v0` to `v5` of stored documents only. Invalid rules
        (cf. :py:func:`validate_rule`) are skipped with a warning. Rules
        stored more than once, or already in the model, are loaded once.

        Args:
            model: Casbin model to load rules into.
        """
        # Not `policy_map`: `clear_policy()` does not clear it on reloads
        loaded: dict[str, set[tuple[str, ...]]] = {}
        for document in self._collection.find():
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
            rule_type = cast(str, ptype)
            assertion = model[rule_type[0]][rule_type]
            if rule_type not in loaded:
                loaded[rule_type] = {tuple(rule) for rule in assertion.policy}
            key = tuple(values)
            if key in loaded[rule_type]:
                continue
            loaded[rule_type].add(key)
            _append_rule(assertion, values)

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
            configured: Whether the rule is set via the app's configuration
                (`access_control.policies`), so that it is managed on
                startup and cannot be changed via the permissions API.

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
        if id is not None:
            document["id"] = id
            self._collection.replace_one({"id": id}, document, upsert=True)
            return id
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
        self._ensure_id_index()
        marked = self._collection.update_many(
            {**_rule_query(ptype, rule), CONFIGURED_FIELD: {"$ne": True}},
            _GRANTED_UPDATE,
        ).matched_count
        inserted = None if marked else self._insert_granted(ptype, rule)
        try:
            self._keep_latest_role(ptype, rule, roles)
        # Also if interrupted, so that the subject is not left with more rights
        except BaseException:
            # Unless granted again concurrently, by a call that succeeds
            if inserted is not None:
                self._remove_inserted(ptype, rule, inserted)
            raise
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
        return self._collection.delete_many(query).deleted_count

    def is_configured(self, id: str) -> bool:
        """Check whether a stored rule is set via the app's configuration.

        Args:
            id: Identifier of the rule.

        Returns:
            Whether a rule with identifier `id` is stored and marked as
            configured.
        """
        return self._has_configured({"id": id})

    def is_configured_rule(self, ptype: str, rule: list[str]) -> bool:
        """Check whether a stored rule with given values is configured.

        Args:
            ptype: Policy type of the rule.
            rule: Values of the rule.

        Returns:
            Whether a rule with exactly these values is stored and marked as
            set via the app's configuration (`access_control.policies`).
        """
        return self._has_configured(_rule_query(ptype, rule))

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
            configured: Whether to get the rules marked as set via the app's
                configuration (`access_control.policies`) or the others.

        Returns:
            Values of the stored rules that match the filter and the mark,
            without duplicates.
        """
        query = _filter_query(ptype, field_index, field_values)
        query[CONFIGURED_FIELD] = True if configured else {"$ne": True}
        rules = dict.fromkeys(
            tuple(_rule_values(document)) for document in self._collection.find(query)
        )
        return [list(rule) for rule in rules]

    def _has_configured(self, query: dict[str, Any]) -> bool:
        """Check whether a stored rule matching a query is configured.

        Args:
            query: Query for stored rules.

        Returns:
            Whether a stored rule matches `query` and is marked as set via
            the app's configuration.
        """
        query = {**query, CONFIGURED_FIELD: True}
        return self._collection.count_documents(query, limit=1) > 0

    def set_configured_policies(self, rules: list[list[str]]) -> None:
        """Make the stored configured rules match the given rules.

        Stored rules marked as configured that are not given, and duplicates
        of configured rules (e.g., added by app instances starting
        concurrently), are removed. Given rules that are stored without the
        mark (e.g., added via the permissions API) are marked; others are
        added. Other stored rules are kept.

        Args:
            rules: Configured rules, each given as its policy type followed
                by its values; they need to be valid (cf.
                :py:func:`validate_rule`).
        """
        missing = {tuple(rule): rule for rule in rules}
        for document in self._collection.find({CONFIGURED_FIELD: True}):
            key = (document.get("ptype"), *_rule_values(document))
            if missing.pop(key, None) is None:
                self._collection.delete_one({"_id": document["_id"]})
                logger.info("Removed configured policy rule '%s'", document.get("id"))
        for ptype, *rule in missing.values():
            if self._collection.update_many(
                _rule_query(ptype, rule), {"$set": {CONFIGURED_FIELD: True}}
            ).matched_count:
                continue
            id = self.save_policy_line(ptype, rule, configured=True)
            logger.info("Added configured policy rule '%s'", id)

    def save_policy(self, model: Model) -> bool:
        """Method to save a casbin model.

        Replaces all stored rules with the rules of the model.

        Args:
            model: Casbin Model which loads from .conf file. For model
                description, cf. https://github.com/casbin/pycasbin/blob/72571\
                5fc04b3f37f26eb4be1ba7007ddf55d1e75/casbin/model/model.py#L23

        Returns:
            True if successfully created.
        """
        self._collection.delete_many({})
        for section_type in ["p", "g"]:
            for ptype, ast in model.model[section_type].items():
                for rule in ast.policy:
                    self.save_policy_line(ptype, rule)
        return True

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
        return results.deleted_count > 0


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
