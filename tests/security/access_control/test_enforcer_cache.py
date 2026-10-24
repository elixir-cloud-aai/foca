"""Tests for the cached Casbin enforcers of app instances."""

import logging
import random
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib.resources import path as resource_path
from pathlib import Path
from typing import Any, ClassVar

import mongomock
import pytest
from casbin import Enforcer
from casbin.rbac.default_role_manager import DomainManager, RoleManager
from flask import Flask
from flask.globals import request_ctx
from pymongo import MongoClient
from pymongo.collection import Collection

from foca.models.config import Config
from foca.security.access_control import (
    add_resource_role,
    is_permitted,
    list_resource_roles,
    remove_resource_role,
)
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    CHANGES_FIELD,
    CHANGES_MARKER_ID,
    DEFAULT_MODEL_FILE,
    MAX_RECORDED_CHANGES,
    RESOURCE_ROLE_MODEL_FILE,
)
from foca.security.access_control.enforcement import (
    _get_role_rules,
    get_enforcer,
    locked_enforcer,
)
from foca.security.access_control.enforcer_cache import (
    EnforcerCache,
    ResourceRoleManager,
    _get_filters,
    is_within_path,
)
from foca.security.access_control.foca_casbin_adapter.adapter import (
    Adapter,
    PolicyChanges,
)

DB_NAME = "foca_test_enforcer_cache"
TASK = "/tasks/1"
ISSUER = "https://login.example.org/oidc/"
# Model whose rules Casbin sorts by priority
PRIORITY_MODEL = """
[request_definition]
r = sub, obj, act

[policy_definition]
p = priority, sub, obj, act, eft

[role_definition]
g = _, _

[policy_effect]
e = priority(p.eft) || deny

[matchers]
m = g(r.sub, p.sub) && r.obj == p.obj && r.act == p.act
"""
# Model whose rules Casbin sorts by subject hierarchy
SUBJECT_PRIORITY_MODEL = """
[request_definition]
r = sub, obj, act

[policy_definition]
p = sub, obj, act, eft

[role_definition]
g = _, _

[policy_effect]
e = subjectPriority(p.eft) || deny

[matchers]
m = g(r.sub, p.sub) && r.obj == p.obj && r.act == p.act
"""


def _model_path(model_file: str) -> str:
    """Get the path of a model file of FOCA."""
    with resource_path(ACCESS_CONTROL_BASE_PATH, model_file) as model:
        return str(model)


RESOURCE_ROLE_MODEL = _model_path(RESOURCE_ROLE_MODEL_FILE)
DEFAULT_MODEL = _model_path(DEFAULT_MODEL_FILE)


@pytest.fixture(
    params=["mongomock", pytest.param("mongodb", marks=pytest.mark.mongodb)]
)
def collection(
    request: pytest.FixtureRequest,
) -> Iterator[Collection[dict[str, Any]]]:
    """Empty policy rule collection, in memory or in a MongoDB server.

    The MongoDB server runs on `localhost:12345` (tests marked `mongodb`).
    """
    if request.param == "mongomock":
        yield mongomock.MongoClient()[DB_NAME]["policy_rules"]
        return
    client: MongoClient[dict[str, Any]] = MongoClient(
        "mongodb://localhost:12345", serverSelectionTimeoutMS=5000
    )
    client.drop_database(DB_NAME)
    yield client[DB_NAME]["policy_rules"]
    client.drop_database(DB_NAME)
    client.close()


@pytest.fixture
def adapter(collection: Collection[dict[str, Any]]) -> Adapter:
    """Adapter of an empty policy rule collection."""
    return Adapter(collection=collection, model=RESOURCE_ROLE_MODEL)


@pytest.fixture
def other_adapter(collection: Collection[dict[str, Any]]) -> Adapter:
    """Adapter of another app instance, for the same collection."""
    return Adapter(collection=collection, model=RESOURCE_ROLE_MODEL)


@pytest.fixture
def cache(adapter: Adapter) -> EnforcerCache:
    """Enforcer cache of the reference model for resource-scoped roles."""
    return EnforcerCache(RESOURCE_ROLE_MODEL, adapter, True)


def _write_model(tmp_path: Path, text: str) -> str:
    """Write a model file and get its path."""
    path = tmp_path / "model.conf"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _count_loads(monkeypatch: pytest.MonkeyPatch, cache: EnforcerCache) -> list[None]:
    """Count the loads of all rules by an enforcer cache."""
    loads: list[None] = []
    load = cache._load

    def counting_load() -> Enforcer:
        loads.append(None)
        return load()

    monkeypatch.setattr(cache, "_load", counting_load)
    return loads


def _g2_rules(enforcer: Enforcer) -> list[list[str]]:
    """Get the `g2` rules of an enforcer, sorted."""
    return sorted(enforcer.get_named_grouping_policy("g2"))


@pytest.mark.parametrize(
    "request_path,resource_path,expected",
    [
        ("/services/ABC", "/services/ABC", True),
        ("/services/ABC/permissions", "/services/ABC", True),
        ("/services/ABC/a/b", "/services/ABC", True),
        ("/services/ABC/", "/services/ABC", True),
        ("/services/ABC", "/services/ABC/", True),
        ("/services/ABC/permissions", "/services/ABC/", True),
        ("/services/ABCD", "/services/ABC", False),
        ("/services/AB", "/services/ABC", False),
        ("/services", "/services/ABC", False),
        ("/services/XYZ/permissions", "/services/ABC", False),
        # the root path only matches itself
        ("/", "/", True),
        ("/services/ABC", "/", False),
    ],
)
def test_is_within_path(request_path: str, resource_path: str, expected: bool) -> None:
    """Request paths are matched against resource paths and their sub-paths."""
    assert is_within_path(request_path, resource_path) is expected


class TestResourceRoleManager:
    """Tests for the role manager of resource-scoped roles."""

    PATHS: ClassVar[list[str]] = [
        "/",
        "/tasks",
        "/tasks/",
        "/tasks/1",
        "/tasks/1/",
        "/tasks/1/logs",
        "/tasks/10",
        "/tasks//1",
        "//tasks",
        "",
        "/other/1",
    ]
    SUBJECTS: ClassVar[list[str]] = ["user:a", "user:b", "group:x"]
    ROLES: ClassVar[list[str]] = ["owner", "maintainer"]

    def _managers(
        self, matching: Callable[[str, str], bool] | None, seed: int
    ) -> tuple[ResourceRoleManager, DomainManager]:
        """Get role managers with the same random links."""
        rng = random.Random(seed)  # noqa: S311 (reproducible test data)
        ours = ResourceRoleManager(10)
        casbins = DomainManager(10)
        if matching is not None:
            ours.add_domain_matching_func(matching)
            casbins.add_domain_matching_func(matching)
        links = [
            (rng.choice(self.SUBJECTS), rng.choice(self.ROLES), rng.choice(self.PATHS))
            for _ in range(15)
        ]
        for link in dict.fromkeys(links):
            ours.add_link(*link)
            casbins.add_link(*link)
        for link in rng.sample(list(dict.fromkeys(links)), 3):
            ours.delete_link(*link)
            casbins.delete_link(*link)
        return ours, casbins

    @pytest.mark.parametrize("seed", range(20))
    @pytest.mark.parametrize(
        "matching", [is_within_path, None, lambda a, b: a.startswith(b)]
    )
    def test_same_links_as_casbin(
        self, seed: int, matching: Callable[[str, str], bool] | None
    ) -> None:
        """Links apply in the same domains as with Casbin's role manager."""
        ours, casbins = self._managers(matching, seed)
        for path in self.PATHS:
            for subject in self.SUBJECTS:
                for role in self.ROLES:
                    assert ours.has_link(subject, role, path) == casbins.has_link(
                        subject, role, path
                    )
                assert sorted(ours.get_roles(subject, path)) == sorted(
                    casbins.get_roles(subject, path)
                )

    def test_no_cached_role_managers(self) -> None:
        """Role managers of domains are not kept."""
        manager = ResourceRoleManager(10)
        manager.add_domain_matching_func(is_within_path)
        manager.add_link("user:a", "owner", TASK)
        assert manager.has_link("user:a", "owner", f"{TASK}/logs")
        assert manager.rm_map == {}

    def test_delete_missing_link(self) -> None:
        """Deleting a link that does not exist fails, as with Casbin."""
        manager = ResourceRoleManager(10)
        manager.add_link("user:a", "owner", TASK)
        with pytest.raises(RuntimeError, match="does not exist"):
            manager.delete_link("user:a", "maintainer", TASK)
        with pytest.raises(RuntimeError, match="does not exist"):
            manager.delete_link("user:a", "owner", "/tasks/2")

    def test_delete_keeps_other_links(self) -> None:
        """Deleting a link keeps the indexes of other links."""
        manager = ResourceRoleManager(10)
        manager.add_domain_matching_func(is_within_path)
        manager.add_link("user:a", "owner", TASK)
        manager.add_link("user:a", "maintainer", TASK)
        manager.add_link("user:b", "owner", TASK)
        manager.add_link("user:b", "owner", f"{TASK}/")
        manager.delete_link("user:a", "owner", TASK)
        assert manager.get_rules(0, "user:a") == [["user:a", "maintainer", TASK]]
        manager.delete_link("user:a", "maintainer", TASK)
        assert manager.get_rules(0, "user:a") == []
        manager.delete_link("user:b", "owner", TASK)
        assert manager.has_link("user:b", "owner", f"{TASK}/logs")
        manager.delete_link("user:b", "owner", f"{TASK}/")
        assert not manager.has_link("user:b", "owner", f"{TASK}/logs")
        assert manager.all_links == {}

    def test_clear(self) -> None:
        """Clearing removes all links and indexes."""
        manager = ResourceRoleManager(10)
        manager.add_domain_matching_func(is_within_path)
        manager.add_link("user:a", "owner", TASK)
        manager.clear()
        assert not manager.has_link("user:a", "owner", TASK)
        assert manager.get_rules(0, "user:a") == []

    @pytest.mark.parametrize(
        ("field_index", "values"),
        [
            (0, ["user:a"]),
            (0, ["user:a", "owner"]),
            (0, ["user:a", "owner", "/tasks/1"]),
            (0, ["user:a", "owner", "/tasks/9"]),
            (0, ["user:z"]),
            (1, ["owner", "/tasks/1"]),
            (2, ["/tasks/1"]),
            (2, ["/tasks/9"]),
        ],
    )
    def test_get_rules(self, field_index: int, values: list[str]) -> None:
        """Rules are listed as by Casbin's filters."""
        enforcer = Enforcer(RESOURCE_ROLE_MODEL)
        manager = ResourceRoleManager(10)
        enforcer.set_named_role_manager("g2", manager)
        for rule in [
            ["user:a", "owner", "/tasks/1"],
            ["user:a", "maintainer", "/tasks/1"],
            ["user:b", "owner", "/tasks/1"],
            ["user:a", "owner", "/tasks/2"],
        ]:
            enforcer.add_named_grouping_policy("g2", *rule)
        expected = enforcer.get_filtered_named_grouping_policy(
            "g2", field_index, *values
        )
        assert sorted(manager.get_rules(field_index, *values) or []) == sorted(expected)

    @pytest.mark.parametrize(
        ("field_index", "values"),
        [(0, ["user:a", ""]), (1, ["owner"]), (0, []), (3, ["x"])],
    )
    def test_get_rules_unsupported(self, field_index: int, values: list[str]) -> None:
        """Filters without index are not answered."""
        manager = ResourceRoleManager(10)
        assert manager.get_rules(field_index, *values) is None


class TestEnforcerCache:
    """Tests for the enforcer cache."""

    def test_is_for(self, adapter: Adapter, other_adapter: Adapter) -> None:
        """Caches are for one model, adapter and setting."""
        cache = EnforcerCache(RESOURCE_ROLE_MODEL, adapter, True)
        assert cache.is_for(RESOURCE_ROLE_MODEL, adapter, True)
        assert not cache.is_for(DEFAULT_MODEL, adapter, True)
        assert not cache.is_for(RESOURCE_ROLE_MODEL, other_adapter, True)
        assert not cache.is_for(RESOURCE_ROLE_MODEL, adapter, False)

    def test_loaded_once(
        self, cache: EnforcerCache, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rules are loaded on first use, and not again without changes."""
        adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        loads = _count_loads(monkeypatch, cache)
        enforcer = cache.get_enforcer()
        assert cache.get_enforcer() is enforcer
        assert len(loads) == 1
        assert enforcer.enforce("user:a", f"{TASK}/logs", "GET") is False
        assert enforcer.has_named_grouping_policy("g2", "user:a", "owner", TASK)

    def test_subpaths(self, adapter: Adapter) -> None:
        """Resource-scoped roles apply to sub-paths if configured."""
        adapter.add_policy("p", "p", ["owner", "/tasks/:id/logs", "GET"])
        adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        covering = EnforcerCache(RESOURCE_ROLE_MODEL, adapter, True).get_enforcer()
        exact = EnforcerCache(RESOURCE_ROLE_MODEL, adapter, False).get_enforcer()
        assert covering.enforce("user:a", f"{TASK}/logs", "GET") is True
        assert exact.enforce("user:a", f"{TASK}/logs", "GET") is False

    def test_changes_of_other_instance_applied(
        self,
        cache: EnforcerCache,
        other_adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Grants and revocations of other app instances apply on next use."""
        other_adapter.add_policy("p", "p", ["owner", "/tasks/:id", "GET"])
        enforcer = cache.get_enforcer()
        loads = _count_loads(monkeypatch, cache)
        assert enforcer.enforce("user:a", TASK, "GET") is False
        other_adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        assert cache.get_enforcer().enforce("user:a", TASK, "GET") is True
        other_adapter.remove_policy("g", "g2", ["user:a", "owner", TASK])
        assert cache.get_enforcer().enforce("user:a", TASK, "GET") is False
        other_adapter.replace_role_rule("g2", ["user:a", "owner", TASK], ["maintainer"])
        assert cache.get_enforcer().enforce("user:a", TASK, "GET") is True
        other_adapter.remove_filtered_policy("g", "g2", 2, TASK)
        assert cache.get_enforcer().enforce("user:a", TASK, "GET") is False
        other_adapter.remove_policy("p", "p", ["owner", "/tasks/:id", "GET"])
        other_adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        assert cache.get_enforcer().enforce("user:a", TASK, "GET") is False
        assert cache.get_enforcer() is enforcer
        assert loads == []

    def test_global_roles_applied(
        self, cache: EnforcerCache, other_adapter: Adapter
    ) -> None:
        """Changes of global roles update the role links."""
        enforcer = cache.get_enforcer()
        other_adapter.add_policy("g", "g", ["user:a", "admin"])
        assert cache.get_enforcer().enforce("user:a", TASK, "DELETE") is True
        other_adapter.remove_policy("g", "g", ["user:a", "admin"])
        assert cache.get_enforcer().enforce("user:a", TASK, "DELETE") is False
        assert cache.get_enforcer() is enforcer

    def test_replaced_rule_applied(
        self, cache: EnforcerCache, other_adapter: Adapter
    ) -> None:
        """Replacing a rule by identifier removes the old rule."""
        id = other_adapter.save_policy_line("g2", ["user:a", "owner", TASK])
        cache.get_enforcer()
        other_adapter.save_policy_line("g2", ["user:b", "owner", TASK], id=id)
        assert _g2_rules(cache.get_enforcer()) == [["user:b", "owner", TASK]]
        assert other_adapter.remove_policy_by_id(id) is True
        assert _g2_rules(cache.get_enforcer()) == []

    @pytest.mark.parametrize(
        "write",
        [
            pytest.param(
                lambda adapter, id: adapter.remove_policy_by_id(id), id="removed"
            ),
            pytest.param(
                lambda adapter, id: adapter.save_policy_line(
                    "g2", ["user:b", "owner", TASK], id=id
                ),
                id="replaced",
            ),
        ],
    )
    def test_write_by_id_recorded_before(
        self,
        cache: EnforcerCache,
        other_adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
        write: Callable[[Adapter, str], Any],
    ) -> None:
        """Rules removed by identifier are removed if recording fails after."""
        id = other_adapter.save_policy_line("g2", ["user:a", "owner", TASK])
        cache.get_enforcer()
        record = other_adapter._record_changes
        calls: list[None] = []

        def failing_after_write(changes: list[dict[str, Any]]) -> None:
            calls.append(None)
            if len(calls) > 1:
                raise RuntimeError("unavailable")
            record(changes)

        monkeypatch.setattr(other_adapter, "_record_changes", failing_after_write)
        with pytest.raises(RuntimeError, match="unavailable"):
            write(other_adapter, id)
        assert ["user:a", "owner", TASK] not in _g2_rules(cache.get_enforcer())

    def test_removed_meanwhile(
        self, other_adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rule removed by another request meanwhile is not removed again."""
        id = other_adapter.save_policy_line("g2", ["user:a", "owner", TASK])
        collection = other_adapter._collection
        find_one = collection.find_one

        def find_and_remove(*args: Any, **kwargs: Any) -> Any:
            found = find_one(*args, **kwargs)
            collection.delete_many({"id": id})
            return found

        monkeypatch.setattr(collection, "find_one", find_and_remove)
        version = other_adapter.get_changes().version
        assert other_adapter.remove_policy_by_id(id) is False
        # Only recorded before removing
        assert other_adapter.get_changes().version == version + 1

    def test_replaced_meanwhile(
        self,
        cache: EnforcerCache,
        other_adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A rule replaced meanwhile is removed, too."""
        id = other_adapter.save_policy_line("g2", ["user:a", "owner", TASK])
        cache.get_enforcer()
        collection = other_adapter._collection
        find_one = collection.find_one

        def find_and_replace(*args: Any, **kwargs: Any) -> Any:
            found = find_one(*args, **kwargs)
            collection.update_one({"id": id}, {"$set": {"v0": "user:b"}})
            return found

        monkeypatch.setattr(collection, "find_one", find_and_replace)
        assert other_adapter.remove_policy_by_id(id) is True
        monkeypatch.undo()
        assert _g2_rules(cache.get_enforcer()) == []

    def test_failed_configured_changes_reloaded(
        self,
        cache: EnforcerCache,
        other_adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All rules are loaded again if configured changes are not recorded."""
        other_adapter.set_configured_policies([["g2", "user:a", "owner", TASK]])
        cache.get_enforcer()
        monkeypatch.setattr(
            other_adapter,
            "_record_configured_changes",
            mock_failing(RuntimeError("unavailable")),
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            other_adapter.set_configured_policies([])
        assert _g2_rules(cache.get_enforcer()) == []

    def test_empty_filter_value_reloaded(
        self,
        cache: EnforcerCache,
        adapter: Adapter,
        other_adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty filter values, wildcards for Casbin, make enforcers reload."""
        other_adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        other_cache = EnforcerCache(RESOURCE_ROLE_MODEL, other_adapter, True)
        other_cache.get_enforcer()
        enforcer = cache.get_enforcer()
        loads = _count_loads(monkeypatch, cache)
        with cache.lock:
            enforcer.remove_filtered_named_grouping_policy("g2", 0, "user:a", "")
        # Stored rules match empty values literally: the rule is kept
        assert _g2_rules(cache.get_enforcer()) == [["user:a", "owner", TASK]]
        assert _g2_rules(other_cache.get_enforcer()) == [["user:a", "owner", TASK]]
        assert len(loads) == 1

    @pytest.mark.parametrize(
        "update",
        [
            lambda enforcer: enforcer.update_policy(
                ["owner", TASK, "GET"], ["owner", TASK, "DELETE"]
            ),
            lambda enforcer: enforcer.update_policies(
                [["owner", TASK, "GET"]], [["owner", TASK, "DELETE"]]
            ),
            lambda enforcer: enforcer.update_filtered_policies(
                [["owner", TASK, "DELETE"]], 0, "owner"
            ),
            lambda enforcer: enforcer.remove_policies([["owner", TASK, "GET"]]),
        ],
    )
    def test_updates_refused(
        self,
        cache: EnforcerCache,
        adapter: Adapter,
        update: Callable[[Enforcer], Any],
    ) -> None:
        """Changes the adapter cannot store are refused."""
        adapter.add_policy("p", "p", ["owner", TASK, "GET"])
        enforcer = cache.get_enforcer()
        with pytest.raises(NotImplementedError, match="not supported"):
            update(enforcer)
        assert enforcer.get_policy() == [["owner", TASK, "GET"]]

    @pytest.mark.parametrize(
        "remove",
        [
            lambda enforcer: enforcer.remove_named_grouping_policy(
                "g2", "user:a", "owner", TASK
            ),
            lambda enforcer: enforcer.remove_filtered_named_grouping_policy(
                "g2", 0, "user:a"
            ),
            lambda enforcer: enforcer.delete_roles_for_user("user:a"),
            lambda enforcer: enforcer.remove_filtered_policy(0, "owner"),
        ],
    )
    def test_failed_removal_reloaded(
        self,
        cache: EnforcerCache,
        adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
        remove: Callable[[Enforcer], Any],
    ) -> None:
        """Rules removed in memory but not stored are loaded again."""
        adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        adapter.add_policy("g", "g", ["user:a", "admin"])
        adapter.add_policy("p", "p", ["owner", TASK, "GET"])
        enforcer = cache.get_enforcer()
        monkeypatch.setattr(
            adapter._collection,
            "delete_many",
            mock_failing(RuntimeError("unavailable")),
        )
        with cache.lock, pytest.raises(RuntimeError, match="unavailable"):
            remove(enforcer)
        monkeypatch.undo()
        assert cache.get_enforcer() is not enforcer
        assert _g2_rules(cache.get_enforcer()) == [["user:a", "owner", TASK]]
        assert cache.get_enforcer().get_policy() == [["owner", TASK, "GET"]]
        assert cache.get_enforcer().get_grouping_policy() == [["user:a", "admin"]]

    def test_failed_removal_of_replaced_enforcer(
        self,
        cache: EnforcerCache,
        adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed removal via an old enforcer keeps the current one."""
        adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        enforcer = cache.get_enforcer()
        adapter.mark_changed()
        current = cache.get_enforcer()
        monkeypatch.setattr(
            adapter._collection,
            "delete_many",
            mock_failing(RuntimeError("unavailable")),
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            enforcer.remove_named_grouping_policy("g2", "user:a", "owner", TASK)
        assert cache.get_enforcer() is current

    def test_configured_rules_applied(
        self,
        cache: EnforcerCache,
        other_adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Replacing configured rules applies the changed rules only."""
        other_adapter.set_configured_policies(
            [["g2", "user:a", "owner", TASK], ["g2", "user:b", "owner", TASK]]
        )
        cache.get_enforcer()
        loads = _count_loads(monkeypatch, cache)
        other_adapter.set_configured_policies(
            [["g2", "user:b", "owner", TASK], ["g2", "user:c", "owner", TASK]]
        )
        assert _g2_rules(cache.get_enforcer()) == [
            ["user:b", "owner", TASK],
            ["user:c", "owner", TASK],
        ]
        other_adapter.set_configured_policies(
            [["g2", "user:b", "owner", TASK], ["g2", "user:c", "owner", TASK]]
        )
        assert len(_g2_rules(cache.get_enforcer())) == 2
        assert loads == []

    def test_many_configured_rules_reloaded(
        self,
        cache: EnforcerCache,
        other_adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All rules are loaded again if many configured rules changed."""
        cache.get_enforcer()
        loads = _count_loads(monkeypatch, cache)
        rules = [
            ["g2", f"user:{index}", "owner", TASK]
            for index in range(MAX_RECORDED_CHANGES + 1)
        ]
        other_adapter.set_configured_policies(rules)
        assert len(_g2_rules(cache.get_enforcer())) == len(rules)
        assert len(loads) == 1

    def test_failed_activation_reloaded(
        self,
        cache: EnforcerCache,
        other_adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All rules are loaded again if configured rules may have changed."""
        cache.get_enforcer()
        loads = _count_loads(monkeypatch, cache)

        def failing_activation(generation: str) -> str:
            raise RuntimeError("interrupted")

        monkeypatch.setattr(other_adapter, "_activate_generation", failing_activation)
        with pytest.raises(RuntimeError, match="interrupted"):
            other_adapter.set_configured_policies([["g2", "user:a", "owner", TASK]])
        cache.get_enforcer()
        assert len(loads) == 1

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param(lambda adapter: adapter.mark_changed(), id="marked"),
            pytest.param(
                lambda adapter: adapter.save_policy(
                    Enforcer(RESOURCE_ROLE_MODEL).get_model()
                ),
                id="saved",
            ),
            pytest.param(
                lambda adapter: [
                    adapter.add_policy("g", "g2", [f"user:{index}", "owner", TASK])
                    for index in range(MAX_RECORDED_CHANGES + 1)
                ],
                id="too many",
            ),
            pytest.param(
                lambda adapter: adapter.changes_collection.drop(), id="recreated"
            ),
            pytest.param(
                lambda adapter: adapter.changes_collection.update_one(
                    {"_id": CHANGES_MARKER_ID},
                    {"$push": {CHANGES_FIELD: "malformed"}, "$inc": {"version": 1}},
                ),
                id="malformed",
            ),
        ],
    )
    def test_reloaded(
        self,
        cache: EnforcerCache,
        other_adapter: Adapter,
        monkeypatch: pytest.MonkeyPatch,
        change: Callable[[Adapter], Any],
    ) -> None:
        """All rules are loaded again if changes cannot be applied."""
        other_adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        enforcer = cache.get_enforcer()
        loads = _count_loads(monkeypatch, cache)
        change(other_adapter)
        other_adapter.add_policy("g", "g2", ["user:b", "owner", TASK])
        assert cache.get_enforcer() is not enforcer
        assert len(loads) == 1
        assert cache.get_enforcer().has_named_grouping_policy(
            "g2", "user:b", "owner", TASK
        )

    @pytest.mark.parametrize(
        ("model", "rule"),
        [
            (PRIORITY_MODEL, ["1", "user:a", TASK, "GET", "allow"]),
            (SUBJECT_PRIORITY_MODEL, ["user:a", TASK, "GET", "allow"]),
        ],
    )
    def test_sorted_rules_reloaded(
        self,
        collection: Collection[dict[str, Any]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        model: str,
        rule: list[str],
    ) -> None:
        """All rules are loaded again for models with sorted rules."""
        path = _write_model(tmp_path, model)
        adapter = Adapter(collection=collection, model=path)
        cache = EnforcerCache(path, adapter, True)
        cache.get_enforcer()
        loads = _count_loads(monkeypatch, cache)
        adapter.add_policy("p", "p", rule)
        assert cache.get_enforcer().enforce("user:a", TASK, "GET") is True
        assert len(loads) == 1

    def test_conditional_roles_reloaded(
        self, cache: EnforcerCache, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All rules are loaded again for conditional role definitions."""
        enforcer = cache.get_enforcer()
        monkeypatch.setitem(enforcer.cond_rm_map, "g2", RoleManager(10))
        loads = _count_loads(monkeypatch, cache)
        adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        cache.get_enforcer()
        assert len(loads) == 1

    def test_unknown_policy_type_skipped(
        self, cache: EnforcerCache, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Changes of policy types that the model lacks are skipped."""
        enforcer = cache.get_enforcer()
        loads = _count_loads(monkeypatch, cache)
        adapter.remove_policy("g", "g3", ["user:a", "owner"])
        assert cache.get_enforcer() is enforcer
        assert loads == []

    def test_unindexed_filter_applied(
        self, cache: EnforcerCache, adapter: Adapter
    ) -> None:
        """Changes by filters without index are applied."""
        adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        adapter.add_policy("g", "g2", ["user:a", "maintainer", TASK])
        cache.get_enforcer()
        adapter.remove_filtered_policy("g", "g2", 1, "owner")
        assert _g2_rules(cache.get_enforcer()) == [["user:a", "maintainer", TASK]]

    def test_failed_load_retried(
        self, cache: EnforcerCache, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed load is retried on next use, without a stale enforcer."""
        cache.get_enforcer()
        adapter.mark_changed()
        monkeypatch.setattr(
            adapter, "load_policy", mock_failing(RuntimeError("unavailable"))
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            cache.get_enforcer()
        assert cache._enforcer is None
        monkeypatch.undo()
        assert cache.get_enforcer() is not None

    def test_failed_update_reloaded(
        self, cache: EnforcerCache, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An enforcer whose update failed is not used again."""
        enforcer = cache.get_enforcer()
        adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        monkeypatch.setattr(
            enforcer.get_model(), "add_policy", mock_failing(RuntimeError("failed"))
        )
        with pytest.raises(RuntimeError, match="failed"):
            cache.get_enforcer()
        monkeypatch.undo()
        assert cache.get_enforcer() is not enforcer
        assert _g2_rules(cache.get_enforcer()) == [["user:a", "owner", TASK]]

    def test_failed_read_keeps_enforcer(
        self, cache: EnforcerCache, adapter: Adapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If changed rules cannot be read, they are read again on next use."""
        enforcer = cache.get_enforcer()
        adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        monkeypatch.setattr(
            adapter, "load_filtered_rules", mock_failing(RuntimeError("unavailable"))
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            cache.get_enforcer()
        monkeypatch.undo()
        assert cache.get_enforcer() is enforcer
        assert _g2_rules(enforcer) == [["user:a", "owner", TASK]]

    def test_rules_not_logged(
        self,
        cache: EnforcerCache,
        adapter: Adapter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Casbin does not log the loaded rules, which contain subjects."""
        adapter.add_policy("g", "g2", ["user:secret", "owner", TASK])
        for name in ["casbin", "casbin.policy", "casbin.role", "casbin.enforcer"]:
            logging.getLogger(name).disabled = False
        with caplog.at_level(logging.DEBUG):
            cache.get_enforcer()
            adapter.add_policy("g", "g2", ["user:other", "owner", TASK])
            cache.get_enforcer().enforce("user:secret", TASK, "GET")
        # Records of Casbin and FOCA, not of the database client
        text = "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.name.split(".")[0] in {"casbin", "foca"}
        )
        assert "user:secret" not in text
        assert "user:other" not in text

    def test_concurrent_use(self, cache: EnforcerCache, other_adapter: Adapter) -> None:
        """Threads use and update the enforcer concurrently."""
        other_adapter.add_policy("p", "p", ["owner", "/tasks/:id", "GET"])
        errors: list[BaseException] = []
        done = threading.Event()

        def use() -> None:
            try:
                while not done.is_set():
                    with cache.lock:
                        cache.get_enforcer().enforce("user:a", TASK, "GET")
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=use) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            for index in range(30):
                rule = [f"user:{index % 3}", "owner", f"/tasks/{index}"]
                other_adapter.add_policy("g", "g2", rule)
                if index % 2:
                    other_adapter.remove_policy("g", "g2", rule)
        finally:
            done.set()
            for thread in threads:
                thread.join()
        assert errors == []
        assert _g2_rules(cache.get_enforcer()) == sorted(
            [f"user:{index % 3}", "owner", f"/tasks/{index}"]
            for index in range(0, 30, 2)
        )


def mock_failing(exc: BaseException) -> Callable[..., Any]:
    """Get a function raising an error."""

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise exc

    return failing


@pytest.mark.parametrize(
    "changes",
    [
        ["malformed"],
        [{"ptype": None}],
        [{"ptype": "", "index": 0, "values": []}],
        [{"ptype": "g2", "index": "0", "values": []}],
        [{"ptype": "g2", "index": 0, "values": "user:a"}],
        [{"ptype": "g2", "index": 0, "values": [1]}],
    ],
)
def test_get_filters_reload(changes: list[Any]) -> None:
    """Changes of all rules and malformed changes need all rules loaded."""
    assert _get_filters(changes) is None


def test_get_filters() -> None:
    """Filters are distinct, in the order changed."""
    change = {"ptype": "g2", "index": 0, "values": ["user:a"]}
    other = {"ptype": "p", "index": 1, "values": [TASK]}
    assert _get_filters([change, other, change]) == [
        ("g2", 0, ("user:a",)),
        ("p", 1, (TASK,)),
    ]


def test_policy_changes_default() -> None:
    """Changes are empty unless read."""
    assert PolicyChanges(epoch=None, version=0).changes == ()


def _create_app(collection: Collection[dict[str, Any]]) -> Flask:
    """Create an app instance with policies in a collection."""
    app = Flask(__name__)
    app.config.foca = Config.model_validate(  # type: ignore[attr-defined]  # dynamic attribute
        {
            "security": {
                "auth": {"issuers": [ISSUER], "audience": ["foca"]},
                "access_control": {"enabled": True, "model": RESOURCE_ROLE_MODEL},
            }
        }
    )
    app.config["CASBIN_MODEL"] = RESOURCE_ROLE_MODEL
    app.config["casbin_adapter"] = Adapter(
        collection=collection, model=RESOURCE_ROLE_MODEL
    )
    return app


@contextmanager
def _as_caller(app: Flask, subject: str, method: str = "GET") -> Iterator[None]:
    """Request context with the claims of a subject, as set by Connexion."""
    with app.test_request_context(TASK, method=method):
        request_ctx.connexion_context = {  # type: ignore[attr-defined]  # set by Connexion
            "token_info": {"claims": {"iss": ISSUER, "sub": subject}}
        }
        yield


class TestAppEnforcer:
    """Tests for the shared enforcers of app instances."""

    @pytest.fixture(autouse=True)
    def _apps(self, collection: Collection[dict[str, Any]]) -> None:
        self.app = _create_app(collection)
        self.other_app = _create_app(collection)
        self.app.config["casbin_adapter"].add_policy(
            "p", "p", ["owner", "/tasks/:id", "GET"]
        )

    def test_shared(self) -> None:
        """Requests of an app instance share its enforcer."""
        with _as_caller(self.app, "alice"):
            enforcer = get_enforcer()
            with locked_enforcer() as locked:
                assert locked is enforcer
        with _as_caller(self.app, "bob"):
            assert get_enforcer() is enforcer
        with _as_caller(self.other_app, "alice"):
            assert get_enforcer() is not enforcer

    def test_locked(self) -> None:
        """Other threads wait while the enforcer is used."""
        acquired: list[bool] = []
        with _as_caller(self.app, "alice"), locked_enforcer():
            cache = self.app.extensions["foca_enforcer_cache"]
            thread = threading.Thread(
                target=lambda: acquired.append(cache.lock.acquire(blocking=False))
            )
            thread.start()
            thread.join()
        assert acquired == [False]

    def test_recreated_for_other_adapter(
        self, collection: Collection[dict[str, Any]]
    ) -> None:
        """The enforcer is created again if the app's adapter changes."""
        with _as_caller(self.app, "alice"):
            enforcer = get_enforcer()
            self.app.config["casbin_adapter"] = Adapter(collection=collection)
            assert get_enforcer() is not enforcer

    def test_grants_and_revocations_across_instances(self) -> None:
        """Grants and revocations apply to the next request of any instance."""
        with _as_caller(self.other_app, "alice"):
            assert is_permitted() is False
        with _as_caller(self.app, "alice"):
            add_resource_role("alice", "owner", TASK)
        with _as_caller(self.other_app, "alice"):
            assert is_permitted() is True
        with _as_caller(self.app, "alice"):
            assert remove_resource_role("alice", "owner", TASK) is True
        with _as_caller(self.other_app, "alice"):
            assert is_permitted() is False
            assert list_resource_roles(TASK) == []

    def test_unindexed_role_rules(self) -> None:
        """Role rules without index are filtered by Casbin."""
        enforcer = Enforcer(DEFAULT_MODEL)
        enforcer.add_named_grouping_policy("g2", "/tasks/1", "tasks")
        assert _get_role_rules(enforcer, "g2", 1, "tasks") == [["/tasks/1", "tasks"]]
