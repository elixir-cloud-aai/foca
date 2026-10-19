"""Casbin rule adapter."""

import logging

from casbin import persist
from casbin.model import Model
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from foca.security.access_control.constants import (
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
        uri: This should be the same requiement as pymongo Client's 'uri'
            parameter. See https://pymongo.readthedocs.io/en/stable/api/pymong\
            o/mongo_client.html#pymongo.mongo_client.MongoClient.
        dbname: Database to store policy.
        collection: Collection of the choosen database. Defaults to
            "casbin_rule".
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

    Raises:
        ValueError: No collection is given.
    """

    def __init__(
        self,
        uri: str,
        dbname: str,
        collection: str | None = "casbin_rule",
        id_charset: str = DEFAULT_ID_CHARSET,
        id_length: int = DEFAULT_ID_LENGTH,
        model: str | None = None,
    ):
        """Create an adapter for Mongodb."""
        if collection is None:
            raise ValueError("No collection for policy rules given")
        client: MongoClient = MongoClient(uri)
        self._collection = client[dbname][collection]
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
        (cf. :py:func:`validate_rule`) are skipped with a warning.

        Args:
            model: Casbin model to load rules into.
        """
        for document in self._collection.find():
            ptype = document.get("ptype")
            values = [
                document.get(f"v{index}") for index in range(MAX_RULE_VALUES)
            ]
            while values and values[-1] is None:
                values.pop()
            try:
                validate_rule(ptype, values, model)
            except ValueError as exc:
                logger.warning(
                    f"Skipping invalid policy rule '{document.get('id')}': "
                    f"{exc}"
                )
                continue
            model.add_policy(ptype[0], ptype, values)

    def save_policy_line(
        self, ptype: str, rule: list[str], id: str | None = None
    ) -> str:
        """Method to save a policy.

        Args:
            ptype: Policy type for the given rule based on the given conf file.
            rule: List of policy attributes.
            id: Identifier of the rule. If given, a stored rule with that
                identifier is replaced. Otherwise, a unique identifier is
                generated.

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
        # Match the rule's values exactly, i.e., without further values, but
        # irrespective of other stored fields (e.g., identifiers)
        query: dict = CasbinRule.from_values(ptype, rule).to_dict()
        for index in range(len(rule), MAX_RULE_VALUES):
            query[f"v{index}"] = {"$exists": False}
        return self._collection.delete_many(query).deleted_count

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

        query = {}
        for index, value in enumerate(field_values):
            query[f"v{index + field_index}"] = value

        query["ptype"] = ptype  # type: ignore[assignment]
        results = self._collection.delete_many(query)
        return results.deleted_count > 0
