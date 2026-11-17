"""Tests for casbin rule object and rule validation."""

from importlib.resources import path as resource_path
from typing import Any

import pytest
from casbin.model import Model

from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    RESOURCE_ROLE_MODEL_FILE,
)
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    CasbinRule,
    InvalidRuleError,
    validate_rule,
    validate_rule_value,
)

# Define data
BASE_RULE_OBJECT = {
    "ptype": "ptype",
    "v0": "v0",
    "v1": "v1",
    "v2": "v2",
    "v3": "v3",
    "v4": "v4",
    "v5": "v5",
}
BASE_RULE_STR_REPRESENTATION = "ptype, v0, v1, v2, v3, v4, v5"


class TestCasbinRule:
    """Tests for `CasbinRule`."""

    def test_initialise_object(self) -> None:
        """Rules are created from fields or from values."""
        test_rule = CasbinRule(**BASE_RULE_OBJECT)
        assert test_rule.ptype == "ptype"
        assert test_rule.v0 == "v0"
        assert test_rule.v1 == "v1"
        assert test_rule.v2 == "v2"
        assert test_rule.v3 == "v3"
        assert test_rule.v4 == "v4"
        assert test_rule.v5 == "v5"
        assert test_rule.to_dict() == BASE_RULE_OBJECT
        assert str(test_rule) == BASE_RULE_STR_REPRESENTATION
        assert test_rule == CasbinRule.from_values(
            "ptype", ["v0", "v1", "v2", "v3", "v4", "v5"]
        )

    def test_unset_values(self) -> None:
        """Unset values are skipped."""
        test_rule = CasbinRule(ptype="g", v0="a", v2="c")
        assert test_rule.values() == ["a", "c"]
        assert test_rule.to_dict() == {"ptype": "g", "v0": "a", "v2": "c"}
        assert str(test_rule) == "g, a, c"


@pytest.fixture
def model() -> Model:
    """Reference model for resource-scoped roles."""
    model = Model()
    with resource_path(ACCESS_CONTROL_BASE_PATH, RESOURCE_ROLE_MODEL_FILE) as _path:
        model.load_model(str(_path))
    return model


class TestValidateRuleValue:
    """Tests for `validate_rule_value()`."""

    @pytest.mark.parametrize(
        "value",
        [
            "alice",
            "user:alice@example.org",
            "/items/:id",
            "(GET)|(POST)",
            "a#",
        ],
    )
    def test_valid(self, value: str) -> None:
        """Valid values are returned."""
        assert validate_rule_value(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            None,
            1,
            ["a"],
            "",
            "a,b",
            "a\nb",
            "a\rb",
            "#a",
            "a" * 1025,
        ],
    )
    def test_invalid(self, value: object) -> None:
        """Invalid values are rejected."""
        with pytest.raises(ValueError):
            validate_rule_value(value)

    def test_max_length(self) -> None:
        """Values of maximum length are valid."""
        assert validate_rule_value("a" * 1024) == "a" * 1024


class TestValidateRule:
    """Tests for `validate_rule()`."""

    @pytest.mark.parametrize(
        "ptype,values",
        [
            ("p", ["*", "/items", "GET"]),
            ("p", ["owner", "/items/:id", "(GET)|(PUT)"]),
            ("g", ["user:alice", "admin"]),
            ("g2", ["user:alice", "owner", "/items/1"]),
        ],
    )
    def test_valid(self, model: Model, ptype: str, values: list[str]) -> None:
        """Valid rules are returned."""
        assert validate_rule(ptype, values, model) == values

    @pytest.mark.parametrize(
        "ptype,values",
        [
            # invalid or undefined policy types
            (None, ["a", "b", "GET"]),
            ("", ["a", "b", "GET"]),
            ("x", ["a", "b", "GET"]),
            ("p ", ["a", "b", "GET"]),
            ("p2", ["a", "b", "GET"]),
            ("g3", ["a", "b"]),
            # wrong number of values
            ("p", []),
            ("p", ["a", "b"]),
            ("g", ["a", "b", "c"]),
            ("p", ["a"] * 7),
            # invalid values
            ("p", ["a,b", "b", "GET"]),
            ("g", ["a", None]),
            # invalid action patterns
            ("p", ["a", "b", "(GET"]),
            ("p", ["a", "b", "*"]),
        ],
    )
    def test_invalid(self, model: Model, ptype: str | None, values: list[Any]) -> None:
        """Invalid rules are rejected."""
        with pytest.raises(ValueError):
            validate_rule(ptype, values, model)

    def test_without_model(self) -> None:
        """Without a model, only values are validated."""
        assert validate_rule("p9", ["a", "(b"]) == ["a", "(b"]
        with pytest.raises(ValueError):
            validate_rule("p9", ["a", "b,c"])


class TestInvalidRuleError:
    """Tests for the field, code and reason of `InvalidRuleError`."""

    @pytest.mark.parametrize(
        "ptype,values,field,code",
        [
            (None, ["a"], "policy_type", "required"),
            (1, ["a"], "policy_type", "invalid-type"),
            ("s3cr3t", ["a"], "policy_type", "pattern-mismatch"),
            ("p", [], None, "wrong-value-count"),
            ("p", ["a", 1, "c"], 1, "invalid-type"),
            ("p", ["a", "b", ""], 2, "too-short"),
            ("p", ["s" * 1025, "b", "c"], 0, "too-long"),
            ("p", ["a", "b,s3cr3t", "c"], 1, "invalid-character"),
            ("p", ["#s3cr3t", "b", "c"], 0, "invalid-character"),
            ("p9", ["a", "b", "c"], "policy_type", "unknown-policy-type"),
            ("p", ["a", "b"], None, "wrong-value-count"),
            ("p", ["a", "b", "(s3cr3t"], 2, "invalid-pattern"),
        ],
    )
    def test_fields(
        self,
        model: Model,
        ptype: Any,
        values: list[Any],
        field: str | int | None,
        code: str,
    ) -> None:
        """Errors name the field and code; their reasons don't quote values."""
        with pytest.raises(ValueError) as exc_info:
            validate_rule(ptype, values, model)
        exc = exc_info.value
        assert isinstance(exc, InvalidRuleError)
        assert (exc.field, exc.code) == (field, code)
        assert "s3cr3t" not in exc.reason

    def test_reason_defaults_to_message(self) -> None:
        """Without a reason, the message is the reason."""
        exc = InvalidRuleError("Bad rule", code="invalid")
        assert (str(exc), exc.reason, exc.field) == ("Bad rule", "Bad rule", None)
