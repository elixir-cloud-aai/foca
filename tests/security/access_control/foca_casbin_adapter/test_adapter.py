"""Tests for `adapter.py` module."""

from pathlib import Path
from typing import ClassVar

import mongomock
import pytest
from casbin import Enforcer, Model
from casbin.model.policy import Policy
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, DuplicateKeyError

from foca.security.access_control.constants import ID_ATTEMPTS
from foca.security.access_control.foca_casbin_adapter import adapter as module
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter

# Constants
DB_NAME = "casbin_test"
DIR = Path(__file__).parent / "test_files"
MODEL_CONF_FILE = str(DIR / "rbac_model.conf")
MODEL_ROLES_CONF_FILE = str(DIR / "rbac_with_resources_roles.conf")
TEST_POLICIES_MODEL_CONF = [
    ("p", "p", ["alice", "data1", "read"]),
    ("p", "p", ["bob", "data2", "write"]),
    ("p", "p", ["data2_admin", "data2", "read"]),
    ("p", "p", ["data2_admin", "data2", "write"]),
    ("g", "g", ["alice", "data2_admin"]),
]
TEST_POLICIES_MODEL_ROLES_CONF = [
    ("p", "p", ["alice", "data1", "write"]),
    ("p", "p", ["alice", "data1", "read"]),
    ("p", "p", ["bob", "data2", "read"]),
    ("p", "p", ["data_group_admin", "data_group", "write"]),
    ("g", "g", ["alice", "data_group_admin"]),
    ("g", "g2", ["data2", "data_group"]),
]


@pytest.fixture(
    params=["mongomock", pytest.param("mongodb", marks=pytest.mark.mongodb)]
)
def policy_collection(request):
    """Empty policy rule collection, in memory or in a MongoDB server on
    `localhost:12345` (tests marked `mongodb`)."""
    if request.param == "mongomock":
        yield mongomock.MongoClient()[DB_NAME]["casbin_rule"]
        return
    client: MongoClient = MongoClient(
        "mongodb://localhost:12345", serverSelectionTimeoutMS=5000
    )
    client.drop_database(DB_NAME)
    yield client[DB_NAME]["casbin_rule"]
    client.drop_database(DB_NAME)


class TestAdapter:
    """Class to test adapter configuration."""

    @pytest.fixture(autouse=True)
    def _policy_collection(self, policy_collection):
        self._collection = policy_collection

    def save_policies(
        self, adapter: Adapter, model: Model, policies: list
    ) -> None:
        """Helper function for saving policies of a given model.

        Args:
            adapter: Casbin mongodb adapter.
            model: Casbin model configuration.
            policies: Policies to be saved.
        """
        model.clear_policy()
        for policy in policies:
            model.add_policy(*policy)
        adapter.save_policy(model)

    def get_enforcer(self, conf_file: str, policies: list) -> Enforcer:
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
        adapter = Adapter(self.collection())
        e = Enforcer(conf_file, adapter)
        model = e.get_model()
        self.save_policies(adapter=adapter, model=model, policies=policies)
        return Enforcer(conf_file, adapter)

    def collection(self):
        """Policy rule collection of the test."""
        return self._collection

    def test_enforcer(self):
        """Test policy enforcer."""
        e = self.get_enforcer(
            conf_file=MODEL_CONF_FILE, policies=TEST_POLICIES_MODEL_CONF
        )

        assert e.enforce("alice", "data1", "read") is True
        assert e.enforce("alice", "data1", "write") is False
        assert e.enforce("bob", "data2", "read") is False
        assert e.enforce("bob", "data2", "write") is True
        assert e.enforce("alice", "data2", "read") is True
        assert e.enforce("alice", "data2", "write") is True

    def test_add_policy(self):
        """Test for adding new policy."""
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

    def test_remove_policy(self):
        """Test for removing policy."""
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

    def test_remove_policy_with_incomplete_rule(self):
        """Test to remove policy with incomplete rule. (Policy should not be
        removed)
        """
        adapter = Adapter(self.collection())
        e = Enforcer(MODEL_ROLES_CONF_FILE, adapter)
        e = self.get_enforcer(
            conf_file=MODEL_ROLES_CONF_FILE,
            policies=TEST_POLICIES_MODEL_ROLES_CONF,
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

    def test_remove_policy_with_empty_rule(self):
        """Test to remove policy with empty rule. (Policy should not be
        removed)
        """
        adapter = Adapter(self.collection())
        e = Enforcer(MODEL_ROLES_CONF_FILE, adapter)
        e = self.get_enforcer(
            conf_file=MODEL_ROLES_CONF_FILE,
            policies=TEST_POLICIES_MODEL_ROLES_CONF,
        )
        e.load_policy()

        assert e.enforce("alice", "data1", "write") is True

        remove_policy = adapter.remove_policy(sec="p", ptype=None, rule=())
        e.load_policy()

        assert remove_policy is False
        assert e.enforce("alice", "data1", "write") is True

    def test_save_policy(self):
        """Test to save policy."""
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

    def test_remove_filtered_policy(self):
        """Test to remove filtered policy definitions."""
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


@pytest.fixture
def mock_adapter() -> Adapter:
    """Adapter with in-memory collection."""
    return Adapter(mongomock.MongoClient().db.policy_rules)


class TestAdapterIdentifiers:
    """Tests for identifiers of stored rules."""

    def test_unique_index(self, mock_adapter):
        mock_adapter.save_policy_line("g", ["a", "b"], id="abcdef")
        indexes = mock_adapter._collection.index_information()
        assert any(
            index.get("unique") and index["key"] == [("id", 1)]
            for index in indexes.values()
        )

    def test_duplicate_stored_ids(self, mock_adapter, caplog):
        """Rules are stored even if stored rules share identifiers."""
        mock_adapter._collection.insert_many(
            [
                {"id": "abcdef", "ptype": "g", "v0": "a", "v1": "b"},
                {"id": "abcdef", "ptype": "g", "v0": "c", "v1": "d"},
            ]
        )
        for rule in [["e", "f"], ["g", "h"]]:
            assert mock_adapter.save_policy_line("g", rule)
        assert mock_adapter._collection.count_documents({}) == 4
        assert caplog.text.count("share identifiers") == 1

    def test_index_retried_after_error(self, mock_adapter, monkeypatch):
        """Index creation is retried if it failed."""
        create_index = mock_adapter._collection.create_index
        calls = []

        def failing_create_index(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise ConnectionFailure("unavailable")
            return create_index(*args, **kwargs)

        monkeypatch.setattr(
            mock_adapter._collection, "create_index", failing_create_index
        )
        with pytest.raises(ConnectionFailure):
            mock_adapter.save_policy_line("g", ["a", "b"])
        mock_adapter.save_policy_line("g", ["a", "b"])
        mock_adapter.save_policy_line("g", ["c", "d"])
        assert len(calls) == 2

    def test_remove_rules_with_extra_fields(self, mock_adapter):
        """Stored rules are removed irrespective of other fields."""
        mock_adapter._collection.insert_many(
            [
                {"ptype": "g", "v0": "a", "v1": "b", "foo": "bar"},
                {"ptype": "g", "v0": "a", "v1": "b", "id": "abcdef"},
                {"ptype": "g", "v0": "a", "v1": "b", "v2": "c"},
                {"ptype": "g", "v0": "a", "v1": "c"},
            ]
        )
        assert mock_adapter.remove_policy("g", "g", ["a", "b"])
        assert sorted(
            [doc.get("v1"), doc.get("v2")]
            for doc in mock_adapter._collection.find()
        ) == [["b", "c"], ["c", None]]
        assert not mock_adapter.remove_policy("g", "g", ["a", "b"])
        assert not mock_adapter.remove_policy("g", "g", [])

    def test_retry_on_collision(self, mock_adapter, monkeypatch):
        mock_adapter.save_policy_line("g", ["a", "b"], id="abcdef")
        ids = iter(["abcdef", "abcdef", "ghijkl"])
        monkeypatch.setattr(module, "generate_id", lambda **kwargs: next(ids))
        assert mock_adapter.save_policy_line("g", ["c", "d"]) == "ghijkl"
        assert mock_adapter._collection.count_documents({}) == 2

    def test_retries_exhausted(self, mock_adapter, monkeypatch):
        mock_adapter.save_policy_line("g", ["a", "b"], id="abcdef")
        calls = []

        def generate_id(**kwargs):
            calls.append(kwargs)
            return "abcdef"

        monkeypatch.setattr(module, "generate_id", generate_id)
        with pytest.raises(DuplicateKeyError):
            mock_adapter.save_policy_line("g", ["c", "d"])
        assert len(calls) == ID_ATTEMPTS
        assert mock_adapter._collection.count_documents({}) == 1

    def test_configured_ids(self, mock_adapter):
        mock_adapter.id_charset = "xy"
        mock_adapter.id_length = 12
        id = mock_adapter.save_policy_line("g", ["a", "b"])
        assert len(id) == 12 and set(id) <= {"x", "y"}

    def test_replace_by_id(self, mock_adapter):
        mock_adapter.save_policy_line("g", ["a", "b"], id="abcdef")
        mock_adapter.save_policy_line("g", ["c", "d"], id="abcdef")
        assert list(
            mock_adapter._collection.find({}, projection={"_id": False})
        ) == [{"ptype": "g", "v0": "c", "v1": "d", "id": "abcdef"}]

    def test_save_policy_no_duplicates(self, mock_adapter):
        enforcer = Enforcer(MODEL_CONF_FILE, mock_adapter)
        enforcer.add_policy("alice", "data1", "read")
        enforcer.add_grouping_policy("alice", "admin")
        enforcer.save_policy()
        enforcer.save_policy()
        collection = mock_adapter._collection
        assert collection.count_documents({}) == 2
        enforcer.load_policy()
        assert enforcer.get_policy() == [["alice", "data1", "read"]]


class _UnsearchableList(list):
    """List that fails on searches, which take linear time."""

    def _fail(self, *args, **kwargs):
        raise AssertionError("Loaded rules searched for duplicates")

    __contains__ = index = count = _fail


class TestLoadPolicy:
    """Tests for loading stored rules."""

    def test_duplicates_loaded_once(self, mock_adapter):
        """Rules stored more than once are loaded once."""
        mock_adapter._collection.insert_many(
            [
                {"id": "a", "ptype": "p", "v0": "a", "v1": "b", "v2": "c"},
                {"id": "b", "ptype": "p", "v0": "a", "v1": "b", "v2": "c"},
                {"ptype": "p", "v0": "a", "v1": "b", "v2": "c", "v3": None},
                {"id": "cccccc", "ptype": "g", "v0": "a", "v1": "b"},
                {"id": "dddddd", "ptype": "g", "v0": "a", "v1": "b"},
                {"id": "eeeeee", "ptype": "g", "v0": "b", "v1": "a"},
            ]
        )
        enforcer = Enforcer(MODEL_CONF_FILE, mock_adapter)
        assert enforcer.get_policy() == [["a", "b", "c"]]
        assert enforcer.get_grouping_policy() == [["a", "b"], ["b", "a"]]
        assert enforcer.get_model()["p"]["p"].policy_map == {"a,b,c": 0}
        assert enforcer.get_model()["g"]["g"].policy_map == {
            "a,b": 0,
            "b,a": 1,
        }

    def test_no_duplicate_scan_per_rule(self, mock_adapter, monkeypatch):
        """Guard against per-rule duplicate scans (quadratic loading)."""

        def fail(*args, **kwargs):
            raise AssertionError("Loaded rules scanned for duplicates")

        for cls in (Model, Policy):
            for name in ("has_policy", "add_policy", "add_policies"):
                monkeypatch.setattr(cls, name, fail)
        mock_adapter._collection.insert_many(
            [
                {"ptype": "p", "v0": f"user{index}", "v1": "b", "v2": "c"}
                for index in range(3)
            ]
            + [{"ptype": "g", "v0": "user0", "v1": "admin"}]
        )
        enforcer = Enforcer(MODEL_CONF_FILE, mock_adapter)
        assert len(enforcer.get_policy()) == 3
        assert enforcer.get_roles_for_user("user0") == ["admin"]
        # Loaded rules are not searched directly either
        model = Model()
        model.load_model(MODEL_CONF_FILE)
        for sec in ("p", "g"):
            for assertion in model[sec].values():
                assertion.policy = _UnsearchableList()
        mock_adapter.load_policy(model)
        assert len(model.get_policy("p", "p")) == 3
        assert model.get_policy("g", "g") == [["user0", "admin"]]

    def test_existing_model_rules_not_duplicated(self, mock_adapter):
        """Existing model rules are not loaded again and stay indexed."""
        mock_adapter._collection.insert_many(
            [
                {"ptype": "p", "v0": "a", "v1": "b", "v2": "c"},
                {"ptype": "p", "v0": "d", "v1": "e", "v2": "f"},
            ]
        )
        model = Model()
        model.load_model(MODEL_CONF_FILE)
        model.add_policy("p", "p", ["a", "b", "c"])
        mock_adapter.load_policy(model)
        assert model.get_policy("p", "p") == [
            ["a", "b", "c"],
            ["d", "e", "f"],
        ]
        assert model["p"]["p"].policy_map == {"a,b,c": 0, "d,e,f": 1}


class TestConfiguredRules:
    """Tests for finding and keeping rules set via the configuration."""

    RULES: ClassVar[list[tuple[list[str], bool]]] = [
        (["user:a", "owner", "/x"], True),
        (["user:b", "owner", "/x"], False),
        (["user:c", "owner", "/x/1"], True),
        (["user:d", "owner", "/y"], True),
    ]

    @pytest.fixture(autouse=True)
    def _rules(self, mock_adapter):
        for rule, configured in self.RULES:
            mock_adapter.save_policy_line("g2", rule, configured=configured)

    def test_is_configured_rule(self, mock_adapter):
        assert mock_adapter.is_configured_rule("g2", ["user:a", "owner", "/x"])
        assert not mock_adapter.is_configured_rule(
            "g2", ["user:b", "owner", "/x"]
        )
        # values must match exactly
        assert not mock_adapter.is_configured_rule("g2", ["user:a", "owner"])
        assert not mock_adapter.is_configured_rule(
            "g", ["user:a", "owner", "/x"]
        )

    def test_is_configured_rule_with_unmarked_copy(self, mock_adapter):
        """A rule stored both with and without the mark is configured."""
        mock_adapter.save_policy_line("g2", ["user:a", "owner", "/x"])
        assert mock_adapter.is_configured_rule("g2", ["user:a", "owner", "/x"])

    def test_get_filtered_rules(self, mock_adapter):
        # stored twice, returned once
        mock_adapter.save_policy_line(
            "g2", ["user:a", "owner", "/x"], configured=True
        )
        assert mock_adapter.get_filtered_rules(
            "g2", 2, "/x", configured=True
        ) == [["user:a", "owner", "/x"]]
        assert mock_adapter.get_filtered_rules(
            "g2", 2, "/x", configured=False
        ) == [["user:b", "owner", "/x"]]
        assert (
            mock_adapter.get_filtered_rules("g2", 0, "user:b", configured=True)
            == []
        )

    def test_get_filtered_rules_several_values(self, mock_adapter):
        assert mock_adapter.get_filtered_rules(
            "g2", 1, "owner", "/x/1", configured=True
        ) == [["user:c", "owner", "/x/1"]]
        assert (
            mock_adapter.get_filtered_rules(
                "g2", 0, "user:a", "owner", "/y", configured=True
            )
            == []
        )

    @pytest.mark.parametrize("field_index", [-1, 6])
    def test_remove_filtered_policy_keep_configured_invalid_filter(
        self, mock_adapter, field_index
    ):
        """Invalid filters remove nothing, also when keeping configured
        rules."""
        assert not mock_adapter.remove_filtered_policy(
            "g", "g2", field_index, "/x", keep_configured=True
        )
        assert not mock_adapter.remove_filtered_policy(
            "g", "g2", 4, "a", "b", "c", keep_configured=True
        )
        assert mock_adapter._collection.count_documents({}) == len(self.RULES)

    def test_remove_filtered_policy_keep_configured(self, mock_adapter):
        assert mock_adapter.remove_filtered_policy(
            "g", "g2", 2, "/x", keep_configured=True
        )
        assert not mock_adapter.remove_filtered_policy(
            "g", "g2", 2, "/x", keep_configured=True
        )
        remaining = mock_adapter._collection.find({}, projection={"_id": 0})
        assert sorted(document["v0"] for document in remaining) == [
            "user:a",
            "user:c",
            "user:d",
        ]
        assert mock_adapter.remove_filtered_policy("g", "g2", 2, "/x")
        assert mock_adapter._collection.count_documents({}) == 2
