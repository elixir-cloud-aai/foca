"""Tests for `adapter.py` module."""

from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar, NoReturn, cast

import mongomock
import pytest
from casbin import Enforcer, Model
from casbin.model.policy import Policy
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import ConnectionFailure, DuplicateKeyError

from foca.security.access_control.constants import (
    CONFIGURED_FIELD,
    GENERATION_MARKER_ID,
    ID_ATTEMPTS,
    LOAD_POLICY_ATTEMPTS,
    MARKER_GENERATION_FIELD,
    RULE_GENERATIONS_FIELD,
)
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


ROLES = ("owner", "maintainer")


def _roles(adapter: Adapter, subject: str = "user:a") -> list[str]:
    """Get roles of a subject on resource `/x`, as loaded."""
    return sorted(
        rule[1]
        for rule in adapter.get_filtered_rules("g2", 0, subject, configured=False)
        + adapter.get_filtered_rules("g2", 0, subject, configured=True)
        if rule[2:] == ["/x"]
    )


class TestReplaceRoleRule:
    """Tests for `Adapter.replace_role_rule()`."""

    @pytest.fixture
    def adapter(self, policy_collection: Collection[dict[str, Any]]) -> Adapter:
        """Adapter with an in-memory or MongoDB collection."""
        return Adapter(policy_collection)

    def test_replace(self, adapter: Adapter) -> None:
        """Rules replace other roles of the subject on the resource."""
        assert adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        assert adapter.replace_role_rule("g2", ["user:a", "maintainer", "/x"], ROLES)
        assert _roles(adapter) == ["maintainer"]
        # granting a role again replaces the others, too
        adapter.save_policy_line("g2", ["user:a", "owner", "/x"])
        assert not adapter.replace_role_rule(
            "g2", ["user:a", "maintainer", "/x"], ROLES
        )
        assert _roles(adapter) == ["maintainer"]
        assert adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        assert _roles(adapter) == ["owner"]
        # stored rules get identifiers
        assert adapter._collection.find_one({"v1": "owner"})["id"]  # type: ignore[index]  # found

    def test_other_rules_kept(self, adapter: Adapter) -> None:
        """Other roles, subjects, resources and rules are kept."""
        others = [
            ["user:a", "auditor", "/x"],
            ["user:b", "owner", "/x"],
            ["user:a", "owner", "/y"],
            ["user:a", "owner", "/x", "extra"],
        ]
        for rule in others:
            adapter.save_policy_line("g2", rule)
        adapter.save_policy_line("g", ["user:a", "owner", "/x"])
        adapter.replace_role_rule("g2", ["user:a", "maintainer", "/x"], ROLES)
        assert adapter._collection.count_documents({}) == len(others) + 2

    def test_configured_rules_kept(self, adapter: Adapter) -> None:
        """Rules set via the configuration are not replaced."""
        adapter.save_policy_line("g2", ["user:a", "owner", "/x"], configured=True)
        assert adapter.replace_role_rule("g2", ["user:a", "maintainer", "/x"], ROLES)
        assert _roles(adapter) == ["maintainer", "owner"]
        # granting the configured role adds an unmarked copy
        assert not adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        assert _roles(adapter) == ["owner", "owner"]

    def test_duplicates_removed(self, adapter: Adapter) -> None:
        """Copies of rules and roles stored before are replaced."""
        for role in ["owner", "owner", "maintainer"]:
            adapter.save_policy_line("g2", ["user:a", role, "/x"])
        assert not adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        assert _roles(adapter) == ["owner"]
        assert adapter._collection.count_documents({}) == 1

    def test_no_window_without_role(
        self, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new rule is stored before the other roles are removed."""
        adapter.replace_role_rule("g2", ["user:a", "maintainer", "/x"], ROLES)
        seen: list[list[str]] = []
        keep_latest = adapter._keep_latest_role

        def observe(*args: Any) -> None:
            seen.append(_roles(adapter))
            keep_latest(*args)

        monkeypatch.setattr(adapter, "_keep_latest_role", observe)
        adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        assert seen == [["maintainer", "owner"]]
        assert _roles(adapter) == ["owner"]

    @pytest.mark.parametrize("roles", [("owner", "maintainer"), ("owner", "owner")])
    def test_concurrent(
        self,
        adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
        roles: tuple[str, str],
    ) -> None:
        """Concurrent replacements leave one role, of the later rule."""
        first, second = roles
        keep_latest = adapter._keep_latest_role
        interleaved = False

        def interleave(*args: Any) -> None:
            # a concurrent request replaces the role after the first one
            # stored its rule
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                adapter.replace_role_rule("g2", ["user:a", second, "/x"], ROLES)
            keep_latest(*args)

        monkeypatch.setattr(adapter, "_keep_latest_role", interleave)
        adapter.replace_role_rule("g2", ["user:a", first, "/x"], ROLES)
        assert _roles(adapter) == [second]

    def test_concurrent_before_cleanup(
        self, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rules stored concurrently before either cleanup leave one role."""
        keep_latest = adapter._keep_latest_role
        pending: list[tuple[Any, ...]] = []

        def defer(*args: Any) -> None:
            pending.append(args)

        monkeypatch.setattr(adapter, "_keep_latest_role", defer)
        adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        adapter.replace_role_rule("g2", ["user:a", "maintainer", "/x"], ROLES)
        assert _roles(adapter) == ["maintainer", "owner"]
        # cleanups in either order keep the later rule
        for args in reversed(pending):
            keep_latest(*args)
        assert _roles(adapter) == ["maintainer"]

    def test_granted_again_during_cleanup(
        self, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rules granted again after being read for removal are kept."""
        adapter.replace_role_rule("g2", ["user:a", "maintainer", "/x"], ROLES)
        delete_many = adapter._collection.delete_many
        interleaved = False

        def interleave(*args: Any, **kwargs: Any) -> Any:
            # a concurrent request grants the role to be removed again
            # after the first one read the rules, before it removed them
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                adapter.replace_role_rule("g2", ["user:a", "maintainer", "/x"], ROLES)
                assert _roles(adapter) == ["maintainer"]
            return delete_many(*args, **kwargs)

        monkeypatch.setattr(adapter._collection, "delete_many", interleave)
        adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        assert interleaved
        assert _roles(adapter) == ["maintainer"]

    def test_cleanup_fails(
        self, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """New rules are removed again if other roles cannot be removed."""
        adapter.replace_role_rule("g2", ["user:a", "maintainer", "/x"], ROLES)

        def fail(*args: Any) -> NoReturn:
            raise ConnectionFailure("database not available")

        monkeypatch.setattr(adapter, "_keep_latest_role", fail)
        with pytest.raises(ConnectionFailure):
            adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        assert _roles(adapter) == ["maintainer"]
        # rules stored before are kept
        with pytest.raises(ConnectionFailure):
            adapter.replace_role_rule("g2", ["user:a", "maintainer", "/x"], ROLES)
        assert _roles(adapter) == ["maintainer"]

    def test_cleanup_fails_after_granted_again(
        self, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rules granted again concurrently are kept if a cleanup fails."""
        adapter.replace_role_rule("g2", ["user:a", "maintainer", "/x"], ROLES)
        keep_latest = adapter._keep_latest_role

        def fail(*args: Any) -> NoReturn:
            # a concurrent request grants the same role, which succeeds
            monkeypatch.setattr(adapter, "_keep_latest_role", keep_latest)
            assert not adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
            raise ConnectionFailure("database not available")

        monkeypatch.setattr(adapter, "_keep_latest_role", fail)
        with pytest.raises(ConnectionFailure):
            adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        assert _roles(adapter) == ["owner"]

    def test_invalid_rule(self, adapter: Adapter) -> None:
        """Invalid rules are rejected."""
        with pytest.raises(ValueError):
            adapter.replace_role_rule("g2", ["user:a", "a,b", "/x"], ROLES)
        with pytest.raises(ValueError, match="three values"):
            adapter.replace_role_rule("g2", ["user:a", "owner"], ROLES)
        assert adapter._collection.count_documents({}) == 0

    def test_retry_on_collision(
        self, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identifiers are generated again on collisions."""
        adapter.save_policy_line("g", ["a", "b"], id="abcdef")
        ids = iter(["abcdef", "ghijkl"])
        monkeypatch.setattr(module, "generate_id", lambda **kwargs: next(ids))
        assert adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        assert adapter._collection.find_one({"v1": "owner"})["id"] == "ghijkl"  # type: ignore[index]  # found

    def test_retries_exhausted(
        self, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Replacing fails if no unique identifier is generated in time."""
        adapter.save_policy_line("g", ["a", "b"], id="abcdef")
        monkeypatch.setattr(module, "generate_id", lambda **kwargs: "abcdef")
        with pytest.raises(DuplicateKeyError):
            adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        assert adapter._collection.count_documents({}) == 1

    def test_stored_concurrently_with_same_id(
        self, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Copies stored concurrently with the same identifier are not used."""
        # stored after checking for copies of the rule, before storing it
        update_many = adapter._collection.update_many

        def store_copy(*args: Any, **kwargs: Any) -> Any:
            result = update_many(*args, **kwargs)
            adapter.save_policy_line("g2", ["user:a", "owner", "/x"], id="abcdef")
            return result

        def fail(*args: Any) -> NoReturn:
            raise ConnectionFailure("database not available")

        monkeypatch.setattr(adapter._collection, "update_many", store_copy)
        ids = iter(["abcdef", "ghijkl"])
        monkeypatch.setattr(module, "generate_id", lambda **kwargs: next(ids))
        monkeypatch.setattr(adapter, "_keep_latest_role", fail)
        with pytest.raises(ConnectionFailure):
            adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        # only the rule stored by the call is removed again
        assert [document["id"] for document in adapter._collection.find()] == ["abcdef"]

    def test_rollback_fails(
        self,
        adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Rules that cannot be removed again are logged."""

        def fail(*args: Any, **kwargs: Any) -> NoReturn:
            raise ConnectionFailure("database not available")

        monkeypatch.setattr(adapter, "_keep_latest_role", fail)
        monkeypatch.setattr(adapter._collection, "delete_one", fail)
        with pytest.raises(ConnectionFailure):
            adapter.replace_role_rule("g2", ["user:a", "owner", "/x"], ROLES)
        assert "could not be removed" in caplog.text
        assert "g2, user:a, owner, /x" in caplog.text

    def test_changed_during_cleanup(
        self, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rules changed or configured after being read are kept."""
        adapter.replace_role_rule("g2", ["user:a", "maintainer", "/x"], ROLES)
        adapter.save_policy_line("g2", ["user:a", "owner", "/x"], id="abcdef")
        delete_many = adapter._collection.delete_many

        def change(*args: Any, **kwargs: Any) -> Any:
            # the rules to remove are marked as configured, or changed
            adapter._collection.update_many(
                {"v1": "maintainer"}, {"$set": {"configured": True}}
            )
            adapter.save_policy_line("g2", ["user:b", "owner", "/x"], id="abcdef")
            return delete_many(*args, **kwargs)

        monkeypatch.setattr(adapter._collection, "delete_many", change)
        adapter.replace_role_rule(
            "g2", ["user:a", "auditor", "/x"], [*ROLES, "auditor"]
        )
        assert adapter._collection.count_documents({}) == 3


# Configured rules before and after replacing them, and of a concurrent app
# instance; and a rule that is not configured
OLD = [["p", "alice", "data1", "read"], ["g", "alice", "admin"]]
NEW = [["p", "alice", "data1", "read"], ["p", "bob", "data2", "write"]]
OTHER = [["g", "carol", "admin"]]
API_RULE = ["p", "dave", "data3", "read"]
# Writes to replace OLD with NEW: tagging rules without generation, adding
# NEW's rules to the new generation (an update of the active copy of the
# kept rule; updates of an active and any other copy, then an insert, of the
# new one), making it active, removing the previous generation, removing the
# unused rule
REPLACE_WRITES = (
    "update_many",
    "update_one",
    "update_one",
    "update_one",
    "insert_one",
)
SWITCH_WRITES = (*REPLACE_WRITES, "find_one_and_update")
ALL_WRITES = (*SWITCH_WRITES, "update_many", "delete_one")


class _HookedCollection:
    """Policy collection calling a hook before some of its methods.

    Args:
        collection: Collection to delegate to.
        hook: Called with the name of the method before each call.
        names: Names of the methods to call the hook for.
    """

    def __init__(
        self,
        collection: Collection[dict[str, Any]],
        hook: Callable[[str], None],
        names: tuple[str, ...] = (
            "insert_one",
            "update_one",
            "update_many",
            "find_one_and_update",
            "delete_one",
            "delete_many",
        ),
    ) -> None:
        self._delegate = collection
        self._hook = hook
        self._names = names

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._delegate, name)
        if name not in self._names:
            return method

        def hooked(*args: Any, **kwargs: Any) -> Any:
            self._hook(name)
            return method(*args, **kwargs)

        return hooked


def _hooked_adapter(
    collection: Collection[dict[str, Any]],
    hook: Callable[[str], None],
    names: tuple[str, ...] | None = None,
) -> Adapter:
    """Get adapter whose collection calls a hook before writes."""
    hooked = (
        _HookedCollection(collection, hook)
        if names is None
        else _HookedCollection(collection, hook, names)
    )
    return Adapter(cast(Collection[dict[str, Any]], hooked))


def _loaded(collection: Collection[dict[str, Any]]) -> set[tuple[str, ...]]:
    """Get the rules loaded for enforcing policies."""
    model = Model()
    model.load_model(MODEL_CONF_FILE)
    Adapter(collection).load_policy(model)
    return {
        (ptype, *rule)
        for sec in ("p", "g")
        for ptype, assertion in model[sec].items()
        for rule in assertion.policy
    }


def _expected(*rules: list[str]) -> set[tuple[str, ...]]:
    """Get rules loaded for configured rules, with the API rule."""
    return {tuple(rule) for rule in [*rules, API_RULE]}


def _check_clean(
    collection: Collection[dict[str, Any]], rules: list[list[str]]
) -> None:
    """Ensure that only the active generation's rules are stored, once."""
    marker = collection.find_one({"_id": GENERATION_MARKER_ID})
    assert marker is not None
    configured = list(collection.find({CONFIGURED_FIELD: True}))
    assert all(
        document[RULE_GENERATIONS_FIELD] == [marker[MARKER_GENERATION_FIELD]]
        for document in configured
    )
    stored = [[doc["ptype"], *module._rule_values(doc)] for doc in configured]
    assert sorted(stored) == sorted(rules)
    # nor are rules left without the mark
    assert _loaded(collection) == _expected(*rules)


class TestConfiguredGenerations:
    """Tests for replacing configured rules atomically."""

    @pytest.fixture(autouse=True)
    def _old(self, policy_collection: Collection[dict[str, Any]]) -> None:
        """Store the old configured rules and a rule that is not configured."""
        self.collection = policy_collection
        Adapter(policy_collection).set_configured_policies(OLD)
        Adapter(policy_collection).save_policy_line(API_RULE[0], API_RULE[1:])

    def test_replaced(self) -> None:
        """Rules are replaced; identifiers of kept rules are kept."""
        kept = self.collection.find_one({"v0": "alice", "ptype": "p"})
        assert kept is not None
        Adapter(self.collection).set_configured_policies(NEW)
        assert _loaded(self.collection) == _expected(*NEW)
        assert self.collection.count_documents({"id": kept["id"]}) == 1
        _check_clean(self.collection, NEW)

    def test_never_mixed(self) -> None:
        """Rules are loaded with the old or the new rules after each write."""
        states: list[set[tuple[str, ...]]] = []
        names: list[str] = []

        def record(name: str) -> None:
            names.append(name)
            states.append(_loaded(self.collection))

        _hooked_adapter(self.collection, record).set_configured_policies(NEW)
        assert tuple(names) == ALL_WRITES
        # before each write, and after the last
        states.append(_loaded(self.collection))
        switch = len(SWITCH_WRITES)
        assert states == [_expected(*OLD)] * switch + [_expected(*NEW)] * (
            len(states) - switch
        )

    @pytest.mark.parametrize("step", range(len(ALL_WRITES)))
    def test_concurrent(self, step: int) -> None:
        """Concurrent replacements leave the rules of the last one to switch.

        The other replacement runs before each write of the first one.
        """
        writes: list[str] = []
        other_switched_first: list[bool] = []

        def other(name: str) -> None:
            if len(writes) == step:
                other_switched_first.append("find_one_and_update" not in writes)
                Adapter(self.collection).set_configured_policies(OTHER)
            writes.append(name)
            assert _loaded(self.collection) in (
                _expected(*OLD),
                _expected(*NEW),
                _expected(*OTHER),
            )

        _hooked_adapter(self.collection, other).set_configured_policies(NEW)
        assert len(other_switched_first) == 1
        last = NEW if other_switched_first[0] else OTHER
        assert _loaded(self.collection) == _expected(*last)
        _check_clean(self.collection, last)

    @pytest.mark.parametrize("step", range(len(ALL_WRITES)))
    def test_failed(self, step: int, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed replacement leaves the old or the new rules in effect.

        Replacing them again removes what the failed one left, once older
        than `STALE_GENERATION_AGE`.
        """
        writes: list[str] = []

        def fail(name: str) -> None:
            if len(writes) == step:
                raise ConnectionFailure("database not available")
            writes.append(name)

        with pytest.raises(ConnectionFailure):
            _hooked_adapter(self.collection, fail).set_configured_policies(NEW)
        switched = step >= len(SWITCH_WRITES)
        assert _loaded(self.collection) == _expected(*(NEW if switched else OLD))
        adapter = Adapter(self.collection)
        assert adapter.is_configured_rule("g", ["alice", "admin"]) is not switched
        assert adapter.is_configured_rule("p", ["bob", "data2", "write"]) is switched
        monkeypatch.setattr(module, "STALE_GENERATION_AGE", timedelta(0))
        adapter.set_configured_policies(NEW)
        assert _loaded(self.collection) == _expected(*NEW)
        _check_clean(self.collection, NEW)

    def test_saved_configured(self) -> None:
        """Rules saved as configured join the active generation.

        They are removed on the next replacement.
        """
        adapter = Adapter(self.collection)
        adapter.save_policy_line("g", ["erin", "admin"], configured=True)
        assert adapter.is_configured_rule("g", ["erin", "admin"])
        assert _loaded(self.collection) == _expected(*OLD, ["g", "erin", "admin"])
        adapter.set_configured_policies(OLD)
        _check_clean(self.collection, OLD)

    def test_marker_created_concurrently(self) -> None:
        """Activating retries if another instance created the marker."""
        self.collection.delete_one({"_id": GENERATION_MARKER_ID})
        adapter = Adapter(self.collection)
        update = self.collection.find_one_and_update
        attempts: list[str] = []

        def created(*args: Any, **kwargs: Any) -> Any:
            attempts.append("activate")
            if len(attempts) == 1:
                raise DuplicateKeyError("marker exists")
            return update(*args, **kwargs)

        adapter._collection = cast(
            Collection[dict[str, Any]],
            _HookedCollection(self.collection, lambda name: None),
        )
        adapter._collection.find_one_and_update = created  # type: ignore[method-assign]
        adapter.set_configured_policies(NEW)
        assert len(attempts) == 2
        assert _loaded(self.collection) == _expected(*NEW)

    def test_marker_conflict(self) -> None:
        """Activating fails if the marker cannot be created or updated."""

        def conflict(*args: Any, **kwargs: Any) -> Any:
            raise DuplicateKeyError("marker exists")

        adapter = Adapter(self.collection)
        adapter._collection = cast(
            Collection[dict[str, Any]],
            _HookedCollection(self.collection, lambda name: None),
        )
        adapter._collection.find_one_and_update = conflict  # type: ignore[method-assign]
        with pytest.raises(DuplicateKeyError):
            adapter.set_configured_policies(NEW)
        assert _loaded(self.collection) == _expected(*OLD)

    def test_save_policy_keeps_configured(self) -> None:
        """Saving a model keeps configured rules, without unmarked copies."""
        model = Model()
        model.load_model(MODEL_CONF_FILE)
        adapter = Adapter(self.collection)
        adapter.load_policy(model)
        model.add_policy("p", "p", ["erin", "data4", "read"])
        model.remove_policy("p", "p", API_RULE[1:])
        adapter.save_policy(model)
        assert _loaded(self.collection) == {
            *(tuple(rule) for rule in OLD),
            ("p", "erin", "data4", "read"),
        }
        unmarked = {CONFIGURED_FIELD: {"$ne": True}, "ptype": {"$exists": True}}
        assert self.collection.count_documents(unmarked) == 1
        adapter.set_configured_policies([])
        assert _loaded(self.collection) == {("p", "erin", "data4", "read")}

    def test_pending_not_in_effect(self) -> None:
        """Rules of a generation that is not active yet are not in effect."""

        def check(name: str) -> None:
            if name == "find_one_and_update":
                adapter = Adapter(self.collection)
                assert not adapter.is_configured_rule("p", ["bob", "data2", "write"])
                assert adapter.get_filtered_rules("p", 0, "bob", configured=True) == []
                assert adapter.get_filtered_rules("p", 0, "bob", configured=False) == []
                assert self.collection.count_documents(adapter.rules_query()) == 3

        _hooked_adapter(self.collection, check).set_configured_policies(NEW)
        assert Adapter(self.collection).is_configured_rule(
            "p", ["bob", "data2", "write"]
        )

    def test_reloaded_if_switched(self) -> None:
        """Rules are loaded again if replaced while loading them."""
        finds: list[str] = []

        def replace(name: str) -> None:
            if not finds:
                Adapter(self.collection).set_configured_policies(NEW)
            finds.append(name)

        model = Model()
        model.load_model(MODEL_CONF_FILE)
        _hooked_adapter(self.collection, replace, names=("find",)).load_policy(model)
        assert len(finds) == 2
        assert model["g"]["g"].policy == []
        assert sorted(model["p"]["p"].policy) == sorted(
            [rule[1:] for rule in [*NEW, API_RULE]]
        )

    def test_reloaded_too_often(self) -> None:
        """Rules are not loaded if replaced during each attempt."""

        def replace(name: str) -> None:
            Adapter(self.collection).set_configured_policies(NEW)

        adapter = _hooked_adapter(self.collection, replace, names=("find",))
        model = Model()
        model.load_model(MODEL_CONF_FILE)
        with pytest.raises(
            RuntimeError, match=f"each of {LOAD_POLICY_ATTEMPTS} attempts"
        ):
            adapter.load_policy(model)

    def test_unknown_generations_removed(self) -> None:
        """Generations not created by FOCA are removed."""
        self.collection.insert_many(
            [
                {
                    "id": "unknw1",
                    "ptype": "g",
                    "v0": "a",
                    "v1": "b",
                    CONFIGURED_FIELD: True,
                    RULE_GENERATIONS_FIELD: [1],
                },
                {
                    "id": "unknw2",
                    "ptype": "g",
                    "v0": "c",
                    "v1": "d",
                    CONFIGURED_FIELD: True,
                    RULE_GENERATIONS_FIELD: ["other"],
                },
            ]
        )
        Adapter(self.collection).set_configured_policies(NEW)
        _check_clean(self.collection, NEW)


def _store_legacy(
    collection: Collection[dict[str, Any]], rule: list[str], id: str
) -> None:
    """Store a configured rule as FOCA 0.16 did, without generations."""
    document = module.CasbinRule.from_values(rule[0], rule[1:]).to_dict()
    collection.insert_one({**document, "id": id, CONFIGURED_FIELD: True})


class TestLegacyConfiguredRules:
    """Tests for configured rules stored by FOCA 0.16, without generations."""

    @pytest.fixture(autouse=True)
    def _legacy(self, policy_collection: Collection[dict[str, Any]]) -> None:
        """Store configured rules as FOCA 0.16 did."""
        self.collection = policy_collection
        for index, rule in enumerate(OLD):
            _store_legacy(policy_collection, rule, f"legcy{index}")
        Adapter(policy_collection).save_policy_line(API_RULE[0], API_RULE[1:])

    def test_in_effect(self) -> None:
        """Configured rules without generation are in effect."""
        assert _loaded(self.collection) == _expected(*OLD)
        assert Adapter(self.collection).is_configured_rule("g", ["alice", "admin"])

    def test_migrated(self) -> None:
        """Rules are replaced atomically, keeping identifiers of kept rules."""
        kept = self.collection.find_one({"v0": "alice", "ptype": "p"})
        assert kept is not None
        states: list[set[tuple[str, ...]]] = []
        _hooked_adapter(
            self.collection, lambda name: states.append(_loaded(self.collection))
        ).set_configured_policies(NEW)
        assert len(states) == len(ALL_WRITES)
        switch = len(SWITCH_WRITES)
        assert states[:switch] == [_expected(*OLD)] * switch
        assert states[switch:] == [_expected(*NEW)] * (len(states) - switch)
        assert _loaded(self.collection) == _expected(*NEW)
        assert self.collection.count_documents({"id": kept["id"]}) == 1
        _check_clean(self.collection, NEW)

    def test_added_later_ignored(self) -> None:
        """Rules without generation stored after migration are not in effect.

        E.g., by an app instance of FOCA 0.16; they are removed on the next
        replacement.
        """
        adapter = Adapter(self.collection)
        adapter.set_configured_policies(NEW)
        _store_legacy(self.collection, ["g", "erin", "admin"], "legcy9")
        assert _loaded(self.collection) == _expected(*NEW)
        assert not adapter.is_configured_rule("g", ["erin", "admin"])
        adapter.set_configured_policies(NEW)
        _check_clean(self.collection, NEW)
