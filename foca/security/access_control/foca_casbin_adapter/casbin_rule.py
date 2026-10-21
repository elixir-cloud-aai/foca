"""Casbin rule class and validation of rules."""

import re
from dataclasses import dataclass
from typing import Any

from casbin.model import Model

# Maximum number of values of a rule (`v0`..`v5`)
MAX_RULE_VALUES = 6
# Maximum length of a rule value
MAX_RULE_VALUE_LENGTH = 1024
# Characters not allowed in rule values, as they would split or comment out
# rules if rules are serialized as lines (e.g., in CSV policy files)
_FORBIDDEN_CHARS = (",", "\r", "\n")
_PTYPE_PATTERN = re.compile(r"[pg]\d*")


def validate_rule_value(value: object) -> str:
    """Validate a value of a policy rule.

    Args:
        value: Value of a policy rule.

    Returns:
        The value.

    Raises:
        ValueError: The value is not a non-empty string, is longer than
            `MAX_RULE_VALUE_LENGTH`, contains a comma or line break, or
            starts with `#`.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("Rule values must be non-empty strings")
    if len(value) > MAX_RULE_VALUE_LENGTH:
        raise ValueError(
            f"Rule values must not be longer than {MAX_RULE_VALUE_LENGTH} characters"
        )
    if any(char in value for char in _FORBIDDEN_CHARS):
        raise ValueError("Rule values must not contain commas or line breaks")
    if value.startswith("#"):
        raise ValueError("Rule values must not start with '#'")
    return value


def validate_rule(
    ptype: object, values: list[Any], model: Model | None = None
) -> list[str]:
    """Validate a policy rule.

    Args:
        ptype: Policy type of the rule (e.g., `p` or `g2`).
        values: Values of the rule.
        model: Casbin model. If provided, the policy type must be defined in
            the model, the number of values must match its definition, and
            values of fields named `act` in policy definitions must be valid
            regular expressions.

    Returns:
        The values.

    Raises:
        ValueError: The rule is invalid.
    """
    if not isinstance(ptype, str) or not _PTYPE_PATTERN.fullmatch(ptype):
        raise ValueError(f"Invalid policy type: {ptype!r}")
    if not 0 < len(values) <= MAX_RULE_VALUES:
        raise ValueError(f"Rules must have between 1 and {MAX_RULE_VALUES} values")
    for value in values:
        validate_rule_value(value)
    if model is None:
        return values
    assertion = model.model.get(ptype[0], {}).get(ptype)
    if assertion is None:
        raise ValueError(f"Policy type not defined in model: {ptype!r}")
    if len(values) != len(assertion.tokens):
        raise ValueError(
            f"Rules of policy type {ptype!r} must have {len(assertion.tokens)} values"
        )
    if ptype[0] == "p":
        for token, value in zip(assertion.tokens, values, strict=True):
            if token == f"{ptype}_act":
                try:
                    re.compile(value)
                except re.error as exc:
                    raise ValueError(
                        f"Invalid action pattern {value!r}: {exc}"
                    ) from exc
    return values


@dataclass
class CasbinRule:
    """Casbin rule, as stored in the database.

    Args:
        ptype: Policy type of the rule, as defined in the model.
        v0: Value of first field of the rule.
        v1: Value of second field of the rule.
        v2: Value of third field of the rule.
        v3: Value of fourth field of the rule.
        v4: Value of fifth field of the rule.
        v5: Value of sixth field of the rule.

    Attributes:
        ptype: Policy type of the rule, as defined in the model.
        v0: Value of first field of the rule.
        v1: Value of second field of the rule.
        v2: Value of third field of the rule.
        v3: Value of fourth field of the rule.
        v4: Value of fifth field of the rule.
        v5: Value of sixth field of the rule.
    """

    ptype: str | None = None
    v0: str | None = None
    v1: str | None = None
    v2: str | None = None
    v3: str | None = None
    v4: str | None = None
    v5: str | None = None

    @classmethod
    def from_values(cls, ptype: str, values: list[str]) -> "CasbinRule":
        """Create rule from policy type and values.

        Args:
            ptype: Policy type of the rule.
            values: Values of the rule, at most `MAX_RULE_VALUES`.

        Returns:
            Rule.
        """
        return cls(ptype, *values)

    def _fields(self) -> dict[str, str | None]:
        """Get fields `v0` to `v5` of the rule."""
        return {
            "v0": self.v0,
            "v1": self.v1,
            "v2": self.v2,
            "v3": self.v3,
            "v4": self.v4,
            "v5": self.v5,
        }

    def values(self) -> list[str]:
        """Get values of the rule.

        Returns:
            Values of fields `v0` to `v5` that are set, in order.
        """
        return [value for value in self._fields().values() if value is not None]

    def to_dict(self) -> dict[str, Any]:
        """Convert rule to dictionary.

        Returns:
            Policy type (key `ptype`) and values (keys `v0` to `v5`) of the
            rule; values that are not set are omitted.
        """
        return {
            "ptype": self.ptype,
            **{
                key: value for key, value in self._fields().items() if value is not None
            },
        }

    def __str__(self) -> str:
        """Return policy type and values as comma-separated string."""
        return ", ".join([str(self.ptype), *self.values()])
