"""Tests of `require_enforced`, `required_roles` and the models' options."""

from importlib.resources import files
from typing import Any
from unittest.mock import MagicMock

import mongomock
import pytest
from flask import Flask
from pydantic import ValidationError

from foca.models.config import AccessControlConfig
from foca.security.access_control import ownership
from foca.security.access_control import register_access_control as module
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    GRANTED_RIGHTS_MODEL_FILE,
    RESOURCE_ROLE_MODEL_FILE,
)
from foca.security.access_control.enforcer_cache import (
    EnforcerCache,
    is_resource_or_custom_method,
    is_within_path_or_custom_method,
)
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter

TASK = "/tasks/1"
GRANTED_MODEL = str(files(ACCESS_CONTROL_BASE_PATH).joinpath(GRANTED_RIGHTS_MODEL_FILE))
RESOURCE_MODEL = str(files(ACCESS_CONTROL_BASE_PATH).joinpath(RESOURCE_ROLE_MODEL_FILE))


class TestRequireEnforced:
    """Tests of `require_enforced`."""

    def test_refused(self) -> None:
        """Without enforcement, the app refuses to start, saying why."""
        with pytest.raises(ValidationError, match="must not run without enforcing"):
            AccessControlConfig(enabled=True, require_enforced=True)

    def test_enforced(self) -> None:
        """With enforcement, nothing happens."""
        assert AccessControlConfig(enabled=True, enforce=True, require_enforced=True)

    def test_default(self) -> None:
        """It's off by default."""
        assert AccessControlConfig(enabled=True).require_enforced is False


class TestRequiredRoles:
    """Tests of `required_roles`."""

    @pytest.mark.parametrize(
        "options",
        [
            {"group_mapping": {"a": "ops"}, "claim_groups": "groups"},
            {"resource_roles": ["owner", "ops"]},
            {"policies": [["g", "user:a", "ops"]]},
            {"policies": [["g2", "user:a", "ops", "/a"]]},
        ],
    )
    def test_exists(self, options: dict[str, Any]) -> None:
        """Roles of mappings, of resources and of rules exist."""
        module._check_required_roles(
            AccessControlConfig(required_roles=["ops"], **options)
        )

    def test_client_mapping(self) -> None:
        """Roles of service clients exist."""
        conf = AccessControlConfig(required_roles=["ops"])
        conf.client_mapping = {"client": "ops"}
        module._check_required_roles(conf)

    @pytest.mark.parametrize(
        "options",
        [
            {},
            {"policies": [["p", "ops", "/a", "GET"]]},
            {"policies": [["g", "ops", "other"]]},
        ],
    )
    def test_missing(self, options: dict[str, Any]) -> None:
        """Otherwise, the app refuses to start, naming the roles."""
        with pytest.raises(ValueError, match=r"'ops'.*required_roles"):
            module._check_required_roles(
                AccessControlConfig(
                    required_roles=["ops"],
                    resource_roles=["owner"],
                    **options,
                )
            )

    def test_default(self) -> None:
        """By default, no role is required."""
        module._check_required_roles(AccessControlConfig())


class TestGrantedRightsModel:
    """Tests of the model without the override of admins."""

    @staticmethod
    def _enforcer(model: str) -> Any:
        """Get an enforcer of a model with rules.

        Args:
            model: Path to the model.

        Returns:
            The enforcer.
        """
        adapter = Adapter(mongomock.MongoClient().db.coll, model=model)
        adapter.add_policy("p", "p", ["admin", "/admin/*", "GET"])
        adapter.add_policy("p", "p", ["owner", "/tasks/:id", "GET"])
        adapter.add_policy("g", "g", ["user:root", "admin"])
        adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        return EnforcerCache(model, adapter, True).get_enforcer()

    def test_packaged_name(self) -> None:
        """The model can be named by the prefix."""
        conf = AccessControlConfig(model="foca:granted_rights_model.conf")
        assert conf.model == GRANTED_MODEL

    def test_unknown_packaged_name(self) -> None:
        """Other names of packaged models are refused."""
        with pytest.raises(ValidationError, match="FOCA ships"):
            AccessControlConfig(model="foca:other.conf")

    def test_admins_only_have_granted_rights(self) -> None:
        """Admins only have the rights that rules grant them."""
        enforcer = self._enforcer(GRANTED_MODEL)
        assert enforcer.enforce("user:root", "/admin/rules", "GET") is True
        assert enforcer.enforce("user:root", TASK, "GET") is False
        assert enforcer.enforce("admin", TASK, "GET") is False

    def test_others_as_in_reference_model(self) -> None:
        """Other subjects have the same rights as in the reference model."""
        enforcer = self._enforcer(GRANTED_MODEL)
        assert enforcer.enforce("user:a", TASK, "GET") is True
        assert enforcer.enforce("user:a", TASK, "PUT") is False
        assert enforcer.enforce("user:b", TASK, "GET") is False

    def test_reference_model_unchanged(self) -> None:
        """In the reference model, admins still may do everything."""
        enforcer = self._enforcer(RESOURCE_MODEL)
        assert enforcer.enforce("user:root", TASK, "GET") is True

    def test_is_admin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Admins aren't special for the helpers with this model only."""
        monkeypatch.setattr(ownership, "_get_current_subjects", lambda: ["user:root"])
        monkeypatch.setattr(
            ownership, "_get_current_policy_subjects", lambda: ["admin"]
        )
        app = Flask(__name__)
        with app.app_context():
            app.config["CASBIN_MODEL"] = GRANTED_MODEL
            assert ownership.is_admin() is False
            app.config["CASBIN_MODEL"] = RESOURCE_MODEL
            assert ownership.is_admin() is True


class TestCustomMethods:
    """Tests of `resource_roles_cover_custom_methods`."""

    @pytest.mark.parametrize(
        ("path", "resource", "within", "same"),
        [
            ("/tasks/1", "/tasks/1", True, True),
            ("/tasks/1:cancel", "/tasks/1", True, True),
            ("/tasks/1/logs", "/tasks/1", True, False),
            ("/tasks/1/logs:tail", "/tasks/1/logs", True, True),
            ("/tasks/1/logs:tail", "/tasks/1", True, False),
            ("/tasks/12:cancel", "/tasks/1", False, False),
            ("/tasks/:cancel", "/tasks", False, False),
            ("/tasks/2:cancel", "/tasks/1", False, False),
            ("/tasks/1:cancel:now", "/tasks/1:cancel", True, True),
        ],
    )
    def test_matching(self, path: str, resource: str, within: bool, same: bool) -> None:
        """Custom methods belong to the resource before them."""
        if path != "/tasks/:cancel":
            assert is_within_path_or_custom_method(path, resource) is within
        assert is_resource_or_custom_method(path, resource) is same

    @pytest.mark.parametrize(
        ("subpaths", "methods", "custom", "below"),
        [
            (True, False, False, True),
            (True, True, True, True),
            (False, True, True, False),
            (False, False, False, False),
        ],
    )
    def test_roles(
        self, subpaths: bool, methods: bool, custom: bool, below: bool
    ) -> None:
        """The roles apply as configured, and off by default."""
        adapter = Adapter(mongomock.MongoClient().db.coll, model=RESOURCE_MODEL)
        adapter.add_policy("p", "p", ["owner", "/tasks/*", "POST"])
        adapter.add_policy("g", "g2", ["user:a", "owner", TASK])
        enforcer = EnforcerCache(
            RESOURCE_MODEL, adapter, subpaths, methods
        ).get_enforcer()
        assert enforcer.enforce("user:a", TASK, "POST") is True
        assert enforcer.enforce("user:a", f"{TASK}:cancel", "POST") is custom
        assert enforcer.enforce("user:a", f"{TASK}/logs", "POST") is below
        assert enforcer.enforce("user:a", "/tasks/2:cancel", "POST") is False

    def test_cache_for_options(self) -> None:
        """A change of the option replaces the cache."""
        cache = EnforcerCache(RESOURCE_MODEL, adapter := MagicMock(), True, True)
        assert cache.is_for(RESOURCE_MODEL, adapter, True, True)
        assert not cache.is_for(RESOURCE_MODEL, adapter, True, False)

    def test_default(self) -> None:
        """It's off by default."""
        conf = AccessControlConfig()
        assert conf.resource_roles_cover_custom_methods is False
