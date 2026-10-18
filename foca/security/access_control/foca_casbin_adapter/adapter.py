"""Casbin rule adapter."""

import logging

from casbin import persist
from casbin.model import Model
from pymongo import MongoClient

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

    Attributes:
        uri: This should be the same requiement as pymongo Client's 'uri'
            parameter. See https://pymongo.readthedocs.io/en/stable/api/pymong\
            o/mongo_client.html#pymongo.mongo_client.MongoClient.
        dbname: Database to store policy.
        collection: Collection of the choosen database. Defaults to
            "casbin_rule".
    """

    def __init__(
        self, uri: str, dbname: str, collection: str | None = "casbin_rule"
    ):
        """Create an adapter for Mongodb."""
        client: MongoClient = MongoClient(uri)
        db = client[dbname]
        assert collection is not None
        self._collection = db[collection]

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

    def save_policy_line(self, ptype: str, rule: list[str]):
        """Method to save a policy.

        Args:
            ptype: Policy type for the given rule based on the given conf file.
            rule: List of policy attributes.

        Returns:
            Identifier of the stored rule.

        Raises:
            ValueError: The rule is invalid (cf. :py:func:`validate_rule`).
        """
        validate_rule(ptype, rule)
        line = CasbinRule(ptype=ptype)
        for index, value in enumerate(rule):
            setattr(line, f"v{index}", value)
        rule_dict: dict = line.dict()
        rule_dict["id"] = generate_id()
        self._collection.insert_one(rule_dict)
        return rule_dict["id"]

    def _delete_policy_lines(self, ptype: str, rule: list[str]) -> int:
        """Method to find a delete policies given a list of policy attributes.

        Args:
            ptype: Policy type for the given rule based on the given conf file.
            rule: List of policy attributes.

        Returns:
            Count of policies deleted.
        """
        line = CasbinRule(ptype=ptype)
        for index, value in enumerate(rule):
            setattr(line, f"v{index}", value)

        # if rule is empty, do nothing
        # else find all given rules and delete them
        if len(rule) == 0:
            return 0
        else:
            line_dict = line.dict()
            line_dict_keys_len = len(line_dict)
            results = self._collection.find(
                filter=line_dict, projection={"id": False}
            )
            to_delete = [
                result["_id"]
                for result in results
                if line_dict_keys_len == len(result.keys()) - 1
            ]
            return self._collection.delete_many(
                {"_id": {"$in": to_delete}}
            ).deleted_count

    def save_policy(self, model: Model) -> bool:
        """Method to save a casbin model.

        Args:
            model: Casbin Model which loads from .conf file. For model
                description, cf. https://github.com/casbin/pycasbin/blob/72571\
                5fc04b3f37f26eb4be1ba7007ddf55d1e75/casbin/model/model.py#L23

        Returns:
            True if successfully created.
        """
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
