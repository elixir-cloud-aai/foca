"""Tests for `adapter.py` module."""

from casbin import Enforcer, Model
from pathlib import Path
from pymongo import MongoClient
from typing import List, Tuple

from foca.access_control.foca_casbin_adapter.adapter import Adapter

# Constants
DIR = Path(__file__).parent / "test_files"
MODEL_CONF_FILE = str(DIR / "rbac_model.conf")
MODEL_ROLES_CONF_FILE = str(DIR / "rbac_with_resources_roles.conf")
TEST_POLICIES_MODEL_CONF = [
    ("p", "p", ["alice", "data1", "read"]),
    ("p", "p", ["bob", "data2", "write"]),
    ("p", "p", ["data2_admin", "data2", "read"]),
    ("p", "p", ["data2_admin", "data2", "write"]),
    ("g", "g", ["alice", "data2_admin"])
]
TEST_POLICIES_MODEL_ROLES_CONF = [
    ("p", "p", ["alice", "data1", "write"]),
    ("p", "p", ["alice", "data1", "read"]),
    ("p", "p", ["bob", "data2", "read"]),
    ("p", "p", ["data_group_admin", "data_group", "write"]),
    ("g", "g", ["alice", "data_group_admin"]),
    ("g", "g2", ["data2", "data_group"])
]


class TestAdapter:
    """Class to test adapter configuration."""

    def save_policies(
        self,
        adapter: Adapter,
        model: Model,
        policy: Tuple
    ) -> None:
        """Helper function for adding policy to a given model.

        Args:
            adapter: Casbin mongodb adapter.
            model: Casbin model configuration.
            policy: New policy to be added.

        Attributes:
            adapter: Casbin mongodb adapter.
            model: Casbin model configuration.
            policy: New policy to be added.
        """

        model.clear_policy()
        model.add_policy(*policy)
        adapter.save_policy(model)

    def get_enforcer(
        self,
        conf_file: str,
        policies: List
    ) -> Enforcer:
        """Helper function to register policy enforcer.

        Args:
            conf_file: Policy model configuration file path.
            policies: List of policies to be registered.

        Attributes:
            conf_file: Policy model configuration file path.
            policies: List of policies to be registered.

        Returns:
            Casbin enforcer object that validates against the registered
            policy model.
        """

        adapter = Adapter("mongodb://localhost:12345", "casbin_test")
        e = Enforcer(conf_file, adapter)
        model = e.get_model()
        for _policy in policies:
            self.save_policies(adapter=adapter, model=model, policy=_policy)
        return Enforcer(conf_file, adapter)

    def clear_db(self, dbname: str):
        """Helper to clear db after each test.

        Args:
            dbname: Database name to be cleared.
        Attributes:
            dbname: Database name to be cleared.
        """

        client = MongoClient("mongodb://localhost:12345")
        client.drop_database(dbname)

    def test_enforcer(self):
        """Test policy enforcer."""

        self.clear_db(dbname="casbin_test")
        e = self.get_enforcer(
            conf_file=MODEL_CONF_FILE, policies=TEST_POLICIES_MODEL_CONF
        )

        assert e.enforce("alice", "data1", "read") is True
        assert e.enforce("alice", "data1", "write") is False
        assert e.enforce("bob", "data2", "read") is False
        assert e.enforce("bob", "data2", "write") is True
        assert e.enforce("alice", "data2", "read") is True
        assert e.enforce("alice", "data2", "write") is True
        self.clear_db(dbname="casbin_test")

    def test_add_policy(self):
        """Test for adding new policy."""

        self.clear_db(dbname="casbin_test")
        e = self.get_enforcer(
            conf_file=MODEL_CONF_FILE, policies=TEST_POLICIES_MODEL_CONF
        )
        adapter = e.get_adapter()
        assert e.enforce("alice", "data1", "write") is False
        assert e.enforce("bob", "data2", "read") is False

        policy1 = adapter.add_policy(
            sec="p", ptype="p", rule=("alice", "data1", "write")
        )
        policy2 = adapter.add_policy(
            sec="p", ptype="p", rule=("bob", "data2", "read")
        )
        e.load_policy()

        assert policy1 is True
        assert policy2 is True
        assert e.enforce("alice", "data1", "read") is True
        assert e.enforce("bob", "data2", "read") is True
        self.clear_db(dbname="casbin_test")

    def test_remove_policy(self):
        """Test for removing policy."""

        self.clear_db(dbname="casbin_test")
        e = self.get_enforcer(
            conf_file=MODEL_CONF_FILE, policies=TEST_POLICIES_MODEL_CONF
        )
        adapter = e.get_adapter()
        assert e.enforce("alice", "data2", "read") is True
        assert e.enforce("alice", "data2", "write") is True

        remove_policy = adapter.remove_policy(
            sec="g", ptype="g", rule=("alice", "data2_admin")
        )
        e.load_policy()

        assert remove_policy is True
        assert e.enforce("alice", "data2", "read") is False
        assert e.enforce("alice", "data2", "write") is False
        self.clear_db(dbname="casbin_test")

    def test_remove_policy_with_incomplete_rule(self):
        """Test to remove policy with incomplete rule. (Policy should not be
        removed)
        """

        self.clear_db(dbname="casbin_test")
        adapter = Adapter("mongodb://localhost:12345", "casbin_test")
        e = Enforcer(MODEL_ROLES_CONF_FILE, adapter)
        e = self.get_enforcer(
            conf_file=MODEL_ROLES_CONF_FILE,
            policies=TEST_POLICIES_MODEL_ROLES_CONF
        )
        e.load_policy()

        assert e.enforce("alice", "data1", "write") is True
        assert e.enforce("alice", "data1", "read") is True
        assert e.enforce("bob", "data2", "read") is True
        assert e.enforce("alice", "data2", "write") is True

        # test remove_policy doesn't remove when given an incomplete policy
        remove_policy = adapter.remove_policy(
            sec="p", ptype="p", rule=("alice", "data1")
        )
        e.load_policy()

        assert remove_policy is False
        assert e.enforce("alice", "data1", "write") is True
        assert e.enforce("alice", "data1", "read") is True
        assert e.enforce("bob", "data2", "read") is True
        assert e.enforce("alice", "data2", "write") is True
        self.clear_db(dbname="casbin_test")

    def test_remove_policy_with_empty_rule(self):
        """Test to remove policy with empty rule. (Policy should not be
        removed)
        """

        self.clear_db(dbname="casbin_test")
        adapter = Adapter("mongodb://localhost:12345", "casbin_test")
        e = Enforcer(MODEL_ROLES_CONF_FILE, adapter)
        e = self.get_enforcer(
            conf_file=MODEL_ROLES_CONF_FILE,
            policies=TEST_POLICIES_MODEL_ROLES_CONF
        )
        e.load_policy()

        assert e.enforce("alice", "data1", "write") is True

        remove_policy = adapter.remove_policy(
            sec="p", ptype=None, rule=()
        )
        e.load_policy()

        assert remove_policy is False
        assert e.enforce("alice", "data1", "write") is True
        self.clear_db(dbname="casbin_test")

    def test_save_policy(self):
        """Test to save policy."""

        self.clear_db(dbname="casbin_test")
        e = self.get_enforcer(
            conf_file=MODEL_CONF_FILE, policies=TEST_POLICIES_MODEL_CONF
        )
        assert e.enforce("alice", "data4", "read") is False

        model = e.get_model()
        model.clear_policy()
        model.add_policy("p", "p", ("alice", "data4", "read"))
        adapter = e.get_adapter()
        adapter.save_policy(model)

        assert e.enforce("alice", "data4", "read") is True
        self.clear_db(dbname="casbin_test")

    def test_remove_filtered_policy(self):
        """Test to remove filtered policy definitions."""

        self.clear_db(dbname="casbin_test")
        e = self.get_enforcer(
            conf_file=MODEL_CONF_FILE, policies=TEST_POLICIES_MODEL_CONF
        )
        adapter = e.get_adapter()
        assert e.enforce("alice", "data1", "read") is True
        assert e.enforce("alice", "data1", "write") is False
        assert e.enforce("bob", "data2", "read") is False
        assert e.enforce("bob", "data2", "write") is True
        assert e.enforce("alice", "data2", "read") is True
        assert e.enforce("alice", "data2", "write") is True

        result = adapter.remove_filtered_policy(
            "g", "g", 6, "alice", "data2_admin"
        )
        e.load_policy()
        assert result is False

        result = adapter.remove_filtered_policy(
            "g", "g", 0, *[f"v{i}" for i in range(7)]
        )
        e.load_policy()
        assert result is False

        result = adapter.remove_filtered_policy(
            "g", "g", 0, "alice", "data2_admin"
        )
        e.load_policy()
        assert result is True
        assert e.enforce("alice", "data1", "read") is True
        assert e.enforce("alice", "data1", "write") is False
        assert e.enforce("bob", "data2", "read") is False
        assert e.enforce("bob", "data2", "write") is True
        assert e.enforce("alice", "data2", "read") is False
        assert e.enforce("alice", "data2", "write") is False
        self.clear_db(dbname="casbin_test")
