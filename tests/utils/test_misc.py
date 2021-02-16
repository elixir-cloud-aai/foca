"""Tests for miscellaneous utility functions."""

import string

import pytest

from foca.utils.misc import generate_id


class TestGenerateId:

    def test_default(self):
        """Use only default arguments."""
        res = generate_id()
        assert isinstance(res, str)

    def test_charset_literal_string(self):
        """Argument to `charset` is non-default literal string."""
        charset = string.digits
        res = generate_id(charset=charset)
        assert set(res) <= set(string.digits)

    def test_charset_literal_string_duplicates(self):
        """Argument to `charset` is non-default literal string with duplicates.
        """
        charset = string.digits + string.digits
        res = generate_id(charset=charset)
        assert set(res) <= set(string.digits)

    def test_charset_evaluates_to_string(self):
        """Argument to `charset` evaluates to string."""
        charset = "''.join([c for c in string.digits])"
        res = generate_id(charset=charset)
        assert set(res) <= set(string.digits)

    def test_charset_evaluates_to_empty_string(self):
        """Argument to `charset` evaluates to non-string."""
        charset = "''.join([])"
        with pytest.raises(TypeError):
            generate_id(charset=charset)

    def test_charset_evaluates_to_non_string(self):
        """Argument to `charset` evaluates to non-string."""
        charset = "1"
        with pytest.raises(TypeError):
            generate_id(charset=charset)

    def test_evaluation_error(self):
        """Evaulation of `length` raises an exception."""
        charset = int
        with pytest.raises(TypeError):
            generate_id(charset=charset)  # type: ignore

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

    def test_length_not_positive(self):
        """Argument to `length` is not a positive integer."""
        length = -1
        with pytest.raises(TypeError):
            generate_id(length=length)
