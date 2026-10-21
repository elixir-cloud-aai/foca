"""Casbin rule adapter."""

import logging
from typing import Any

from casbin import persist
from casbin.model import Assertion, Model
from casbin.model.policy import DEFAULT_SEP
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from foca.security.access_control.constants import (
    CONFIGURED_FIELD,
    DEFAULT_ID_CHARSET,
    DEFAULT_ID_LENGTH,
    ID_ATTEMPTS,
)
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    MAX_RULE_VALUES,
    CasbinRule,
    validate_rule,
)
from foca.utils.misc import generate_id

logger = logging.getLogger(__name__)


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
        collection: Collection,
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
                f"created, as rules in collection '{self._collection.name}' "
                "share identifiers; identifiers of new rules are not "
                "guaranteed to be unique. Delete or re-create the rules "
                f"with duplicate identifiers and restart the app: {exc}"
            )
        # Other errors propagate, so that the next write tries again
        self._id_index_created = True

    def load_policy(self, model: Model):
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
                    f"Skipping invalid policy rule '{document.get('id')}': "
                    f"{exc}"
                )
                continue
            assertion = model[ptype[0]][ptype]
            if ptype not in loaded:
                loaded[ptype] = {tuple(rule) for rule in assertion.policy}
            key = tuple(values)
            if key in loaded[ptype]:
                continue
            loaded[ptype].add(key)
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
            document["id"] = generate_id(
                charset=self.id_charset, length=self.id_length
            )
            try:
                self._collection.insert_one(document)
                return document["id"]
            except DuplicateKeyError:
                attempts_left -= 1
                if not attempts_left:
                    raise
                logger.debug("Generated rule identifier exists; retrying")
                document.pop("_id", None)

    def _delete_policy_lines(self, ptype: str, rule: list[str]) -> int:
        """Method to find a delete policies given a list of policy attributes.

        Args:
            ptype: Policy type for the given rule based on the given conf file.
            rule: List of policy attributes.

        Returns:
            Count of policies deleted.
        """
        if not rule:
            return 0
        return self._collection.delete_many(
            _rule_query(ptype, rule)
        ).deleted_count

    def is_configured(self, id: str) -> bool:
        """Check whether a stored rule is set via the app's configuration.

        Args:
            id: Identifier of the rule.

        Returns:
            Whether a rule with identifier `id` is stored and marked as
            configured.
        """
        query = {"id": id, CONFIGURED_FIELD: True}
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
                logger.info(
                    f"Removed configured policy rule '{document.get('id')}'"
                )
        for ptype, *rule in missing.values():
            if self._collection.update_many(
                _rule_query(ptype, rule), {"$set": {CONFIGURED_FIELD: True}}
            ).matched_count:
                continue
            id = self.save_policy_line(ptype, rule, configured=True)
            logger.info(f"Added configured policy rule '{id}'")

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

    def remove_policy(self, sec: str, ptype: str, rule: list[str]):
        """Remove policy rules from mongodb(duplicate rules are also removed).

        Args:
            sec: Section corresponding which the rule will be added.
            ptype: Policy type for which casbin rule will be removed.
            rule: Casbin rule list definition to be removed.

        Returns:
            Number of policies removed.
        """
        deleted_count = self._delete_policy_lines(ptype, rule)
        return deleted_count > 0

    def remove_filtered_policy(
        self, sec: str, ptype: str, field_index: int, *field_values: list[str]
    ):
        """Remove policy rules that match the filter from the storage.

        This is part of the Auto-Save feature.

        Args:
            sec: Section corresponding which the rule will be added.
            ptype: Policy type for which casbin rule will be removed.
            field_index: The policy index at which the field_values begin
                filtering. Its range is [0, 5]
            field_values: A list of rules to filter policy.

        Returns:
           True if success.
        """
        if not (0 <= field_index <= 5):
            return False
        if not (1 <= field_index + len(field_values) <= 6):
            return False

        query: dict[str, Any] = {"ptype": ptype}
        for index, value in enumerate(field_values):
            query[f"v{index + field_index}"] = value

        results = self._collection.delete_many(query)
        return results.deleted_count > 0


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
    # Re-check when bumping pycasbin: 2.8.0 is the only release in the
    # allowed range `>=2.8,<3` (per PyPI).
    assertion.policy.append(values)
    assertion.policy_map[DEFAULT_SEP.join(values)] = len(assertion.policy) - 1


def _rule_query(ptype: str, rule: list[str]) -> dict:
    """Get query matching stored rules with exactly the given values.

    Args:
        ptype: Policy type of the rule.
        rule: Values of the rule.

    Returns:
        Query matching the rule's values exactly, i.e., without further
        values, but irrespective of other stored fields (e.g., identifiers
        or the configured mark).
    """
    query: dict = CasbinRule.from_values(ptype, rule).to_dict()
    # Match fields that are not set or null, as when loading rules
    for index in range(len(rule), MAX_RULE_VALUES):
        query[f"v{index}"] = None
    return query


def _rule_values(document: dict) -> list:
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
