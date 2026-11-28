"""Unit tests for the rules that the permissions API refuses."""

from importlib.resources import as_file, files

import pytest
from casbin.model import Model

from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_MODEL_FILE,
    RESOURCE_ROLE_MODEL_FILE,
)
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    InvalidRuleError,
)
from foca.security.access_control.refused_rules import (
    _ANY_CHARS_ITEM,
    _parse_value,
    check_refused,
    expand_refused_paths,
    inheriting_roles,
    may_apply_to,
    path_fields,
    validate_refused_path,
)

TASKS = "/ga4gh/tes/v1/tasks/*"


def _model(name: str) -> Model:
    """Load a model that FOCA ships.

    Args:
        name: File name of the model.

    Returns:
        The model.
    """
    model = Model()
    with as_file(files(ACCESS_CONTROL_BASE_PATH).joinpath(name)) as path:
        model.load_model(str(path))
    return model


@pytest.fixture
def model() -> Model:
    """Reference model for resource-scoped roles."""
    return _model(RESOURCE_ROLE_MODEL_FILE)


class TestValidateRefusedPath:
    """Tests for validating refused paths."""

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/tasks/*",
            "/tasks/:id",
            "/tasks/:id:cancel",
            "/v1.0/tasks",
            "{base_path}",
            "{base_path}/tasks/*",
        ],
    )
    def test_valid(self, path: str) -> None:
        """Absolute `keyMatch2` patterns are valid, also after the placeholder."""
        assert validate_refused_path(path) == path

    @pytest.mark.parametrize(
        "path,match",
        [
            ("", "absolute"),
            ("tasks/*", "absolute"),
            ("{base_path}tasks", "absolute"),
            ("/api{base_path}/tasks", "only start with"),
            ("{base_path}/a{base_path}", "only start with"),
            *(
                (f"/tasks/{char}", "keyMatch2")
                for char in ["(a)", "[a]", "{id}", "a|b", "\\d", "^a", "a$", "a+", "a?"]
            ),
            ("/tasks*", "after '/'"),
            ("/tasks/a*b", "after '/'"),
            ("/tasks/a,b", "commas"),
            ("/tasks/a b", "whitespace"),
        ],
    )
    def test_invalid(self, path: str, match: str) -> None:
        """Other paths are refused."""
        with pytest.raises(ValueError, match=match):
            validate_refused_path(path)


def test_expand_refused_paths() -> None:
    """The placeholder is replaced by each base path; the root by `/`."""
    assert expand_refused_paths(
        ["/a", "{base_path}/tasks/*", "{base_path}"], ["/api/v1", ""]
    ) == ["/a", "/api/v1/tasks/*", "/tasks/*", "/api/v1", "/"]


class TestMayApplyTo:
    """Tests for checking whether values of rules apply to refused paths."""

    @pytest.mark.parametrize(
        "value",
        [
            # patterns and paths matched by the refused path
            "/ga4gh/tes/v1/tasks/:id",
            "/ga4gh/tes/v1/tasks/1",
            "/ga4gh/tes/v1/tasks/1:cancel",
            "/ga4gh/tes/v1/tasks/:id:cancel",
            "/ga4gh/tes/v1/tasks/:id/logs",
            "/ga4gh/tes/v1/tasks/",
            # patterns matching more
            "/ga4gh/*",
            "/ga4gh/tes/v1/:collection/:id",
            "/ga4gh/tes/v1/:a*/b",
            "/:a.*",
            "*",
            ".*",
            "/ga4gh/tes/v1/task*",
            "/ga4gh/tes/v1/tasks?",
            "/ga4gh/tes/v1/task./1",
            "/ga4gh/tes/v1/tasks/{id}",
            "/ga4gh/tes/v1/{collection}/1",
            "/ga4gh/tes/v1/tasks/1{",
            "/ga4gh/tes/v1/t+/1",
            # regular expressions that can't be read character by character
            "/ga4gh/tes/v1/(tasks)/:id",
            "/ga4gh/tes/v1/[t]asks/1",
            "/other|/ga4gh/tes/v1/tasks/1",
            "^/ga4gh/tes/v1/tasks/1$",
            "/ga4gh/tes/v1/\\w+/1",
        ],
    )
    def test_patterns(self, value: str) -> None:
        """Patterns that may match a refused path apply to it."""
        assert may_apply_to(value, TASKS) is True

    @pytest.mark.parametrize(
        "value",
        [
            "/ga4gh/tes/v1/tasks",
            "/ga4gh/tes/v1/service-info",
            "/ga4gh/tes/v1/:collection",
            "/ga4gh/tes/v1/tasks:x",
            "/ga4gh/tes/v1/task.",
            "/other/*",
            "/other/:id",
            "user:alice",
            "owner",
        ],
    )
    def test_patterns_not(self, value: str) -> None:
        """Patterns that match no refused path don't apply to it."""
        assert may_apply_to(value, TASKS) is False

    @pytest.mark.parametrize(
        "value",
        [
            "/ga4gh/tes/v1/tasks/1",
            "/ga4gh/tes/v1/tasks/1/",
            "/ga4gh/tes/v1/tasks",
            "/ga4gh/tes/v1/tasks/",
            "/ga4gh/tes/v1",
            "/ga4gh",
            "/",
        ],
    )
    def test_resources(self, value: str) -> None:
        """Resources apply to refused paths below them."""
        assert may_apply_to(value, TASKS, within=True) is True

    @pytest.mark.parametrize(
        "value", ["/ga4gh/tes/v1/service-info", "/ga4gh/tes/v2", "/ga4gh/tes/v1/t"]
    )
    def test_resources_not(self, value: str) -> None:
        """Resources apply to no refused path elsewhere."""
        assert may_apply_to(value, TASKS, within=True) is False

    def test_custom_method_of_resource(self) -> None:
        """Resources apply to refused custom methods of them."""
        refused = "/tasks/1:cancel"
        assert may_apply_to("/tasks/1", refused, within=True) is True
        assert may_apply_to("/tasks/1", refused) is False

    @pytest.mark.parametrize(
        "refused,expected",
        [
            ("/tasks/:id", True),
            ("/tasks/:id/logs", False),
            ("/v1.0/tasks/1", False),
            ("/tasks", False),
        ],
    )
    def test_refused_patterns(self, refused: str, expected: bool) -> None:
        """Refused paths are `keyMatch2` patterns, `.` matching itself."""
        assert may_apply_to("/tasks/1:cancel", refused) is expected

    def test_literal_dot(self) -> None:
        """Dots of refused paths match themselves only."""
        assert may_apply_to("/v1x0/tasks/1", "/v1.0/tasks/*") is False
        assert may_apply_to("/v1.0/tasks/1", "/v1.0/tasks/*") is True


class TestRunsOfAnyCharacters:
    """Tests for keeping the automata of long patterns small."""

    @pytest.mark.parametrize("value", [".*" * 500, "a*" * 500, ".?" * 500, "+" * 900])
    def test_merged(self, value: str) -> None:
        """Runs of any characters are one item."""
        assert _parse_value(value) == [_ANY_CHARS_ITEM]

    def test_long_value_without_overlap(self) -> None:
        """Long values that don't overlap are decided, too."""
        value = "/" + ".*" * 511 + "/"
        assert may_apply_to(value, "/ga4gh/tes/v1/tasks/:id") is False


class TestInheritingRoles:
    """Tests for finding the roles that inherit refused roles."""

    def test_inheriting(self) -> None:
        """Subjects and roles inheriting roles directly or indirectly count."""
        links = [
            ["team-lead", "owner"],
            ["user:alice", "team-lead", "/tasks/1"],
            ["user:bob", "admin"],
            ["orphan"],
        ]
        assert inheriting_roles(["owner"], links) == {
            "owner",
            "team-lead",
            "user:alice",
        }

    def test_none(self) -> None:
        """Without links, only the roles count."""
        assert inheriting_roles(["owner"], []) == {"owner"}


class TestPathFields:
    """Tests for finding the values of rules that are paths."""

    def test_resource_role_model(self, model: Model) -> None:
        """Objects of policy rules and resources of resource roles are paths."""
        assert path_fields("p", model) == [(1, False)]
        assert path_fields("g", model) == []
        assert path_fields("g2", model) == [(2, True)]

    def test_default_model(self) -> None:
        """Groupings with two fields have no paths."""
        model = _model(DEFAULT_MODEL_FILE)
        assert path_fields("p", model) == [(1, False)]
        assert path_fields("g2", model) == []

    def test_custom_model(self) -> None:
        """Policy definitions without `obj` have no paths."""
        model = Model()
        model.load_model_from_text(
            "[request_definition]\nr = sub, res, act\n"
            "[policy_definition]\np = sub, res, act\np2 = sub, obj\n"
            "[policy_effect]\ne = some(where (p.eft == allow))\n"
            "[matchers]\nm = r.sub == p.sub\n"
        )
        assert path_fields("p", model) == []
        assert path_fields("p2", model) == [(1, False)]


class TestCheckRefused:
    """Tests for refusing rules."""

    @pytest.mark.parametrize(
        "ptype,values",
        [
            ("p", ["admin", "/ga4gh/tes/v1/tasks", "GET"]),
            ("g", ["user:alice", "admin"]),
            ("g2", ["user:alice", "maintainer", "/ga4gh/tes/v1/service-info"]),
        ],
    )
    def test_allowed(self, model: Model, ptype: str, values: list[str]) -> None:
        """Rules of other types, roles and paths are allowed."""
        check_refused(
            ptype, values, model, policy_types=["p2"], roles=["owner"], paths=[TASKS]
        )

    def test_nothing_refused(self, model: Model) -> None:
        """By default, nothing is refused."""
        check_refused("g2", ["user:alice", "owner", "/ga4gh/tes/v1/tasks/1"], model)

    @pytest.mark.parametrize(
        "ptype,values,options,code,field",
        [
            (
                "g2",
                ["user:alice", "maintainer", "/other"],
                {"policy_types": ["g2"]},
                "refused-policy-type",
                "policy_type",
            ),
            (
                "g",
                ["user:alice", "owner"],
                {"roles": ["owner"]},
                "refused-role",
                1,
            ),
            (
                "g2",
                ["user:alice", "owner", "/other"],
                {"roles": ["owner"]},
                "refused-role",
                1,
            ),
            (
                "p",
                ["admin", "/ga4gh/tes/v1/tasks/:id", "GET"],
                {"paths": ["/other", TASKS]},
                "refused-path",
                1,
            ),
            (
                "g2",
                ["user:alice", "maintainer", "/ga4gh/tes/v1"],
                {"paths": [TASKS]},
                "refused-path",
                2,
            ),
        ],
    )
    def test_refused(
        self,
        model: Model,
        ptype: str,
        values: list[str],
        options: dict[str, list[str]],
        code: str,
        field: str | int,
    ) -> None:
        """Refused rules name the refused field, without values in the reason."""
        with pytest.raises(InvalidRuleError) as exc:
            check_refused(ptype, values, model, **options)
        assert (exc.value.code, exc.value.field) == (code, field)
        assert not any(value in exc.value.reason for value in values[1:])
