"""Tests for miscellaneous utility functions."""

import string

import pytest

from foca.utils.misc import generate_id


class TestGenerateId:
    def test_default(self):
        """Use only default arguments."""
        res = generate_id()
        assert isinstance(res, str)
        assert set(res) <= set(string.ascii_letters + string.digits)

    def test_charset_literal_string(self):
        """Argument to `charset` is non-default literal string."""
        charset = string.digits
        res = generate_id(charset=charset)
        assert set(res) <= set(string.digits)

    def test_charset_literal_string_duplicates(self):
        """Argument to `charset` is literal string with duplicates."""
        charset = string.digits + string.digits
        res = generate_id(charset=charset)
        assert set(res) <= set(string.digits)

    def test_charset_not_evaluated(self):
        """Argument to `charset` is used literally, not evaluated."""
        charset = "''.join([])"
        res = generate_id(charset=charset, length=20)
        assert set(res) <= set(charset)

    def test_charset_digit_string(self):
        """Argument to `charset` is a string of a single digit."""
        res = generate_id(charset="1", length=3)
        assert res == "111"

    @pytest.mark.parametrize(
        "charset",
        [
            "string.ascii_uppercase + string.digits",
            "string.digits",
            "''.join([string.ascii_letters, string.digits])",
        ],
    )
    def test_charset_expression(self, charset):
        """Argument to `charset` is a former `string` module expression."""
        with pytest.raises(ValueError, match="no longer evaluated"):
            generate_id(charset=charset)

    def test_charset_empty(self):
        """Argument to `charset` is an empty string."""
        with pytest.raises(TypeError):
            generate_id(charset="")

    def test_charset_not_string(self):
        """Argument to `charset` is not a string."""
        with pytest.raises(TypeError):
            generate_id(charset=int)  # type: ignore

    def test_length(self):
        """Non-default argument to `length`."""
        length = 1
        res = generate_id(length=length)
        assert len(res) == length

    def test_length_not_int(self):
        """Argument to `length` is not an integer."""
        length = ""
        with pytest.raises(TypeError):
            generate_id(length=length)  # type: ignore

    def test_length_bool(self):
        """Argument to `length` is a boolean."""
        with pytest.raises(TypeError):
            generate_id(length=True)

    def test_length_not_positive(self):
        """Argument to `length` is not a positive integer."""
        length = -1
        with pytest.raises(TypeError):
            generate_id(length=length)
