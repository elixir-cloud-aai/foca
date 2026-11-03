"""Tests for miscellaneous utility functions."""

import string
from typing import Annotated, Literal

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ImportString,
    ValidationError,
    model_validator,
)

from foca.utils.misc import describe_validation_error, generate_id


class _Settings(BaseModel):
    """Settings for tests of `describe_validation_error()`.

    Attributes:
        required: Required field.
        limit: Limit.
    """

    model_config = ConfigDict(extra="forbid")

    required: str
    limit: int = 1


class _Refusing(BaseModel):
    """Model with an error that has no location."""

    @model_validator(mode="after")
    def refuse(self) -> "_Refusing":
        """Refuse all values.

        Raises:
            ValueError: Always.
        """
        raise ValueError("refused")


def test_describe_validation_error() -> None:
    """Locations and messages are described, inputs are not."""
    with pytest.raises(ValidationError) as info:
        _Settings.model_validate({"apikey": "s3cr3t", "limit": "s3cr3t"})
    text = describe_validation_error(info.value)
    assert "required: Field required" in text
    assert "apikey: Extra inputs are not permitted" in text
    assert "limit: " in text
    assert "s3cr3t" not in text


class _Kind(BaseModel):
    """A member of a discriminated union.

    Attributes:
        kind: Discriminator.
    """

    kind: Literal["a"]


class _OtherKind(BaseModel):
    """Another member of a discriminated union.

    Attributes:
        kind: Discriminator.
    """

    kind: Literal["b"]


class _Quoting(BaseModel):
    """Model with errors whose messages of pydantic quote the input.

    Attributes:
        union: Discriminated union.
        path: Python path.
    """

    union: Annotated[_Kind | _OtherKind, Field(discriminator="kind")] | None = None
    path: ImportString[object] | None = None


@pytest.mark.parametrize(
    ("data", "location"),
    [
        ({"union": {"kind": "s3cr3t"}}, "union"),
        ({"path": "s3cr3t"}, "path"),
    ],
)
def test_describe_validation_error_quoted_input(
    data: dict[str, object], location: str
) -> None:
    """Messages of pydantic that quote the input are not passed on."""
    with pytest.raises(ValidationError) as info:
        _Quoting.model_validate(data)
    assert "s3cr3t" in str(info.value.errors())
    text = describe_validation_error(info.value)
    assert text.startswith(f"{location}")
    assert "s3cr3t" not in text


def test_describe_validation_error_other_errors() -> None:
    """Other errors are described by their type, as they may show the input."""
    assert describe_validation_error(KeyError("s3cr3t")) == "KeyError"


def test_describe_validation_error_without_location() -> None:
    """Errors of whole models have only their message."""
    with pytest.raises(ValidationError) as info:
        _Refusing.model_validate({})
    assert describe_validation_error(info.value) == "Value error, refused"


class TestGenerateId:
    """Tests for `generate_id()`."""

    def test_default(self) -> None:
        """Use only default arguments."""
        res = generate_id()
        assert isinstance(res, str)
        assert set(res) <= set(string.ascii_letters + string.digits)

    def test_charset_literal_string(self) -> None:
        """Argument to `charset` is non-default literal string."""
        charset = string.digits
        res = generate_id(charset=charset)
        assert set(res) <= set(string.digits)

    def test_charset_literal_string_duplicates(self) -> None:
        """Argument to `charset` is literal string with duplicates."""
        charset = string.digits + string.digits
        res = generate_id(charset=charset)
        assert set(res) <= set(string.digits)

    def test_charset_not_evaluated(self) -> None:
        """Argument to `charset` is used literally, not evaluated."""
        charset = "''.join([])"
        res = generate_id(charset=charset, length=20)
        assert len(res) == 20
        assert set(res) <= set(charset)

    @pytest.mark.parametrize(
        "charset",
        [
            "string.abc",
            "my_string.xyz",
            "substring.digits",
        ],
    )
    def test_charset_literal_with_dot(self, charset: str) -> None:
        """Literal charset resembling attribute access is used literally."""
        res = generate_id(charset=charset, length=20)
        assert len(res) == 20
        assert set(res) <= set(charset)

    def test_charset_digit_string(self) -> None:
        """Argument to `charset` is a string of a single digit."""
        res = generate_id(charset="1", length=3)
        assert res == "111"

    @pytest.mark.parametrize(
        "charset",
        [
            "string.ascii_uppercase + string.digits",
            "string.digits",
            "''.join([string.ascii_letters, string.digits])",
            "string . ascii_uppercase+string .digits",
        ],
    )
    def test_charset_expression(self, charset: str) -> None:
        """Argument to `charset` is a former `string` module expression."""
        with pytest.raises(ValueError, match="no longer evaluated"):
            generate_id(charset=charset)

    def test_charset_empty(self) -> None:
        """Argument to `charset` is an empty string."""
        with pytest.raises(TypeError):
            generate_id(charset="")

    def test_charset_not_string(self) -> None:
        """Argument to `charset` is not a string."""
        with pytest.raises(TypeError):
            generate_id(charset=int)  # type: ignore[arg-type]  # invalid on purpose

    def test_length(self) -> None:
        """Non-default argument to `length`."""
        length = 1
        res = generate_id(length=length)
        assert len(res) == length

    def test_length_not_int(self) -> None:
        """Argument to `length` is not an integer."""
        length = ""
        with pytest.raises(TypeError):
            generate_id(length=length)  # type: ignore[arg-type]  # invalid on purpose

    def test_length_bool(self) -> None:
        """Argument to `length` is a boolean."""
        with pytest.raises(TypeError):
            generate_id(length=True)

    def test_length_not_positive(self) -> None:
        """Argument to `length` is not a positive integer."""
        length = -1
        with pytest.raises(TypeError):
            generate_id(length=length)
