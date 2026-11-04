"""Tests of `require_enforced`, `required_roles` and the models' options."""

from importlib.resources import files
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import mongomock
import pytest
from flask import Flask
from pydantic import ValidationError

from foca.models.config import AccessControlConfig, SecurityConfig
from foca.security.access_control import ownership
from foca.security.access_control import register_access_control as module
from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_MODEL_FILE,
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
DEFAULT_MODEL = str(files(ACCESS_CONTROL_BASE_PATH).joinpath(DEFAULT_MODEL_FILE))
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

    @pytest.mark.parametrize(
        "options",
        [{"require_enforced": True, "enforce": True}, {"required_roles": ["a"]}],
    )
    def test_needs_access_control(self, options: dict[str, Any]) -> None:
        """Without access control, the options are refused."""
        with pytest.raises(ValidationError, match=r"require 'access_control\.enabled'"):
            SecurityConfig(access_control=AccessControlConfig(**options))


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

    def test_malformed_rule(self) -> None:
        """Too short rules don't get in the way, and are refused later on."""
        with pytest.raises(ValueError, match="required_roles"):
            module._check_required_roles(
                AccessControlConfig(required_roles=["ops"], policies=[["g", "a"]])
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

    def test_path_model(self, tmp_path: Path) -> None:
        """Models can still be given as paths."""
        path = tmp_path / "model.conf"
        path.touch()
        assert AccessControlConfig.model_validate({"model": path}).model == str(path)

    def test_same_as_reference_model(self) -> None:
        """The models only differ in the override of admins."""

        def _definitions(path: str) -> list[str]:
            """Get the lines of a model that aren't comments."""
            with open(path, encoding="utf-8") as model:
                return [line for line in model if not line.startswith("#")]

        assert [
            line.replace('g(r.sub, "admin") || ', "")
            for line in _definitions(RESOURCE_MODEL)
        ] == _definitions(GRANTED_MODEL)

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

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            (RESOURCE_MODEL, True),
            (GRANTED_MODEL, False),
            # Not for resource-scoped roles, so admins are as they always were
            (DEFAULT_MODEL, True),
        ],
    )
    def test_admin_override_detected(self, model: str, expected: bool) -> None:
        """The override is taken from the model."""
        assert ownership._admins_have_all_rights(model) is expected

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("g = _, _\n", "g = _, _ # global\n"),
            ("m = ", 'm = (g(r.sub, "admin") && r.act == "GET") || '),
            ("m = ", 'm = !g(r.sub, "admin") && '),
        ],
    )
    def test_edited_copies(self, tmp_path: Path, old: str, new: str) -> None:
        """Edited copies without the plain override fail closed."""
        path = tmp_path / "copy.conf"
        text = Path(GRANTED_MODEL).read_text()
        assert old in text
        path.write_text(text.replace(old, new, 1))
        assert ownership._admins_have_all_rights(str(path)) is False

    def test_override_on_next_line(self, tmp_path: Path) -> None:
        """The override is found in matchers over several lines."""
        path = tmp_path / "copy.conf"
        path.write_text(
            Path(RESOURCE_MODEL).read_text().replace("m = g(", "m = \\\n  g(")
        )
        assert ownership._admins_have_all_rights(str(path)) is True

    def test_other_name_of_request_field(self, tmp_path: Path) -> None:
        """The override is found whatever the request field is called."""
        path = tmp_path / "copy.conf"
        path.write_text(
            Path(RESOURCE_MODEL)
            .read_text()
            .replace("r.sub", "r.user")
            .replace("r = sub,", "r = user,")
        )
        assert ownership._admins_have_all_rights(str(path)) is True

    def test_no_model(self) -> None:
        """Without a model, as without access control, nothing is limited."""
        assert ownership._admins_have_all_rights(None) is True

    def test_copy_of_model(self, tmp_path: Path) -> None:
        """A copy of the model fails closed, too."""
        path = tmp_path / "copy.conf"
        path.write_text(Path(GRANTED_MODEL).read_text())
        assert ownership._admins_have_all_rights(str(path)) is False


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
            ("/tasks/1:cancel", "/tasks/1/", True, True),
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

    def test_trailing_slash(self) -> None:
        """Trailing slashes of resources are ignored, whatever the options."""
        adapter = Adapter(mongomock.MongoClient().db.coll, model=RESOURCE_MODEL)
        adapter.add_policy("p", "p", ["owner", "/tasks/*", "POST"])
        adapter.add_policy("g", "g2", ["user:a", "owner", f"{TASK}/"])
        for subpaths in (True, False):
            enforcer = EnforcerCache(
                RESOURCE_MODEL, adapter, subpaths, True
            ).get_enforcer()
            assert enforcer.enforce("user:a", f"{TASK}:cancel", "POST") is True

    def test_cache_for_options(self) -> None:
        """A change of the option replaces the cache."""
        cache = EnforcerCache(RESOURCE_MODEL, adapter := MagicMock(), True, True)
        assert cache.is_for(RESOURCE_MODEL, adapter, True, True)
        assert not cache.is_for(RESOURCE_MODEL, adapter, True, False)

    def test_default(self) -> None:
        """It's off by default."""
        conf = AccessControlConfig()
        assert conf.resource_roles_cover_custom_methods is False
