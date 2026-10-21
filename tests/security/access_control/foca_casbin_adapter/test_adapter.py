"""Tests for `adapter.py` module."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, NoReturn

import mongomock
import pytest
from casbin import Enforcer, Model
from casbin.model.policy import Policy
from pymongo import MongoClient
from pymongo.collection import Collection
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
def policy_collection(
    request: pytest.FixtureRequest,
) -> Iterator[Collection[dict[str, Any]]]:
    """Empty policy rule collection, in memory or in a MongoDB server.

    The MongoDB server runs on `localhost:12345` (tests marked `mongodb`).
    """
    if request.param == "mongomock":
        yield mongomock.MongoClient()[DB_NAME]["casbin_rule"]
        return
    client: MongoClient[dict[str, Any]] = MongoClient(
        "mongodb://localhost:12345", serverSelectionTimeoutMS=5000
    )
    client.drop_database(DB_NAME)
    yield client[DB_NAME]["casbin_rule"]
    client.drop_database(DB_NAME)


class TestAdapter:
    """Class to test adapter configuration."""

    @pytest.fixture(autouse=True)
    def _policy_collection(self, policy_collection: Collection[dict[str, Any]]) -> None:
        self._collection = policy_collection

    def save_policies(
        self,
        adapter: Adapter,
        model: Model,
        policies: list[tuple[str, str, list[str]]],
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

    def get_enforcer(
        self, conf_file: str, policies: list[tuple[str, str, list[str]]]
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
        adapter = Adapter(self.collection())
        e = Enforcer(conf_file, adapter)
        model = e.get_model()
        self.save_policies(adapter=adapter, model=model, policies=policies)
        return Enforcer(conf_file, adapter)

    def collection(self) -> Collection[dict[str, Any]]:
        """Policy rule collection of the test."""
        return self._collection

    def test_enforcer(self) -> None:
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

    def test_add_policy(self) -> None:
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
        policy2 = adapter.add_policy(sec="p", ptype="p", rule=("bob", "data2", "read"))
        e.load_policy()

        assert policy1 is True
        assert policy2 is True
        assert e.enforce("alice", "data1", "read") is True
        assert e.enforce("bob", "data2", "read") is True

    def test_remove_policy(self) -> None:
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

    def test_remove_policy_keeps_configured(self) -> None:
        """Configured copies of a rule are not removed."""
        adapter = Adapter(collection=self._collection)
        adapter.save_policy_line("g", ["a", "b"], configured=True)
        adapter.save_policy_line("g", ["a", "b"])
        assert adapter.remove_policy("g", "g", ["a", "b"]) is True
        assert adapter.remove_policy("g", "g", ["a", "b"]) is False
        documents = list(self._collection.find({}))
        assert len(documents) == 1
        assert documents[0]["configured"] is True

    def test_remove_policy_with_incomplete_rule(self) -> None:
        """Test to remove policy with incomplete rule.

        Policy should not be removed.
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
            sec="p", ptype="p", rule=["alice", "data1"]
        )
        e.load_policy()

        assert remove_policy is False
        assert e.enforce("alice", "data1", "write") is True
        assert e.enforce("alice", "data1", "read") is True
        assert e.enforce("bob", "data2", "read") is True
        assert e.enforce("alice", "data2", "write") is True

    def test_remove_policy_with_empty_rule(self) -> None:
        """Test to remove policy with empty rule.

        Policy should not be removed.
        """
        adapter = Adapter(self.collection())
        e = Enforcer(MODEL_ROLES_CONF_FILE, adapter)
        e = self.get_enforcer(
            conf_file=MODEL_ROLES_CONF_FILE,
            policies=TEST_POLICIES_MODEL_ROLES_CONF,
        )
        e.load_policy()

        assert e.enforce("alice", "data1", "write") is True

        remove_policy = adapter.remove_policy(
            sec="p",
            ptype=None,  # type: ignore[arg-type]  # invalid policy type
            rule=[],
        )
        e.load_policy()

        assert remove_policy is False
        assert e.enforce("alice", "data1", "write") is True

    def test_save_policy(self) -> None:
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

    def test_remove_filtered_policy(self) -> None:
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

        result = adapter.remove_filtered_policy("g", "g", 6, "alice", "data2_admin")
        e.load_policy()
        assert result is False

        result = adapter.remove_filtered_policy(
            "g", "g", 0, *[f"v{i}" for i in range(7)]
        )
        e.load_policy()
        assert result is False

        result = adapter.remove_filtered_policy("g", "g", 0, "alice", "data2_admin")
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

    def test_unique_index(self, mock_adapter: Adapter) -> None:
        """Identifiers of stored rules have a unique index."""
        mock_adapter.save_policy_line("g", ["a", "b"], id="abcdef")
        indexes = mock_adapter._collection.index_information()
        assert any(
            index.get("unique") and index["key"] == [("id", 1)]
            for index in indexes.values()
        )

    def test_duplicate_stored_ids(
        self, mock_adapter: Adapter, caplog: pytest.LogCaptureFixture
    ) -> None:
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

    def test_index_retried_after_error(
        self, mock_adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Index creation is retried if it failed."""
        create_index = mock_adapter._collection.create_index
        calls: list[tuple[Any, ...]] = []

        def failing_create_index(*args: Any, **kwargs: Any) -> str:
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

    def test_remove_rules_with_extra_fields(self, mock_adapter: Adapter) -> None:
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
            [doc.get("v1"), doc.get("v2")] for doc in mock_adapter._collection.find()
        ) == [["b", "c"], ["c", None]]
        assert not mock_adapter.remove_policy("g", "g", ["a", "b"])
        assert not mock_adapter.remove_policy("g", "g", [])

    def test_retry_on_collision(
        self, mock_adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identifiers are generated again on collisions."""
        mock_adapter.save_policy_line("g", ["a", "b"], id="abcdef")
        ids = iter(["abcdef", "abcdef", "ghijkl"])
        monkeypatch.setattr(module, "generate_id", lambda **kwargs: next(ids))
        assert mock_adapter.save_policy_line("g", ["c", "d"]) == "ghijkl"
        assert mock_adapter._collection.count_documents({}) == 2

    def test_retries_exhausted(
        self, mock_adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saving fails if no unique identifier is generated in time."""
        mock_adapter.save_policy_line("g", ["a", "b"], id="abcdef")
        calls: list[dict[str, Any]] = []

        def generate_id(**kwargs: Any) -> str:
            calls.append(kwargs)
            return "abcdef"

        monkeypatch.setattr(module, "generate_id", generate_id)
        with pytest.raises(DuplicateKeyError):
            mock_adapter.save_policy_line("g", ["c", "d"])
        assert len(calls) == ID_ATTEMPTS
        assert mock_adapter._collection.count_documents({}) == 1

    def test_configured_ids(self, mock_adapter: Adapter) -> None:
        """Identifiers follow the configured characters and length."""
        mock_adapter.id_charset = "xy"
        mock_adapter.id_length = 12
        id = mock_adapter.save_policy_line("g", ["a", "b"])
        assert len(id) == 12 and set(id) <= {"x", "y"}

    def test_replace_by_id(self, mock_adapter: Adapter) -> None:
        """Rules saved with the identifier of a stored rule replace it."""
        mock_adapter.save_policy_line("g", ["a", "b"], id="abcdef")
        mock_adapter.save_policy_line("g", ["c", "d"], id="abcdef")
        assert list(mock_adapter._collection.find({}, projection={"_id": False})) == [
            {"ptype": "g", "v0": "c", "v1": "d", "id": "abcdef"}
        ]

    def test_save_policy_no_duplicates(self, mock_adapter: Adapter) -> None:
        """Saving policies repeatedly does not duplicate stored rules."""
        enforcer = Enforcer(MODEL_CONF_FILE, mock_adapter)
        enforcer.add_policy("alice", "data1", "read")
        enforcer.add_grouping_policy("alice", "admin")
        enforcer.save_policy()
        enforcer.save_policy()
        collection = mock_adapter._collection
        assert collection.count_documents({}) == 2
        enforcer.load_policy()
        assert enforcer.get_policy() == [["alice", "data1", "read"]]


class _UnsearchableList(list[list[str]]):
    """List that fails on searches, which take linear time."""

    def _fail(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("Loaded rules searched for duplicates")

    __contains__ = index = count = _fail


class TestLoadPolicy:
    """Tests for loading stored rules."""

    def test_duplicates_loaded_once(self, mock_adapter: Adapter) -> None:
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

    def test_no_duplicate_scan_per_rule(
        self, mock_adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard against per-rule duplicate scans (quadratic loading)."""

        def fail(*args: Any, **kwargs: Any) -> NoReturn:
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

    def test_existing_model_rules_not_duplicated(self, mock_adapter: Adapter) -> None:
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
    def _rules(self, mock_adapter: Adapter) -> None:
        for rule, configured in self.RULES:
            mock_adapter.save_policy_line("g2", rule, configured=configured)

    def test_is_configured_rule(self, mock_adapter: Adapter) -> None:
        """Only rules stored with the mark are configured rules."""
        assert mock_adapter.is_configured_rule("g2", ["user:a", "owner", "/x"])
        assert not mock_adapter.is_configured_rule("g2", ["user:b", "owner", "/x"])
        # values must match exactly
        assert not mock_adapter.is_configured_rule("g2", ["user:a", "owner"])
        assert not mock_adapter.is_configured_rule("g", ["user:a", "owner", "/x"])

    def test_is_configured_rule_with_unmarked_copy(self, mock_adapter: Adapter) -> None:
        """A rule stored both with and without the mark is configured."""
        mock_adapter.save_policy_line("g2", ["user:a", "owner", "/x"])
        assert mock_adapter.is_configured_rule("g2", ["user:a", "owner", "/x"])

    def test_get_filtered_rules(self, mock_adapter: Adapter) -> None:
        """Rules are filtered by a value and by the mark."""
        # stored twice, returned once
        mock_adapter.save_policy_line("g2", ["user:a", "owner", "/x"], configured=True)
        assert mock_adapter.get_filtered_rules("g2", 2, "/x", configured=True) == [
            ["user:a", "owner", "/x"]
        ]
        assert mock_adapter.get_filtered_rules("g2", 2, "/x", configured=False) == [
            ["user:b", "owner", "/x"]
        ]
        assert mock_adapter.get_filtered_rules("g2", 0, "user:b", configured=True) == []

    def test_get_filtered_rules_several_values(self, mock_adapter: Adapter) -> None:
        """Rules are filtered by consecutive values."""
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
        self, mock_adapter: Adapter, field_index: int
    ) -> None:
        """Invalid filters remove nothing, also when keeping configured rules."""
        assert not mock_adapter.remove_filtered_policy(
            "g", "g2", field_index, "/x", keep_configured=True
        )
        assert not mock_adapter.remove_filtered_policy(
            "g", "g2", 4, "a", "b", "c", keep_configured=True
        )
        assert mock_adapter._collection.count_documents({}) == len(self.RULES)

    def test_remove_filtered_policy_keep_configured(
        self, mock_adapter: Adapter
    ) -> None:
        """Configured rules are kept when removing rules, if requested."""
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
