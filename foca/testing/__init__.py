"""Helpers for the tests of FOCA apps.

- :py:class:`Client`: a test client with the API of `httpx`, and
  :py:func:`request_context`, which sets up a request without HTTP (cf.
  :py:mod:`foca.testing.client`).
- :py:mod:`foca.testing.issuer`: a local identity provider, for testing
  authentication as in production.
- :py:mod:`foca.testing.pytest_plugin`: a pytest plugin with a network guard, a
  MongoDB fixture, fixtures for a test client and for the identity provider.
"""

from foca.testing.client import Client, Response, request_context

__all__ = ["Client", "Response", "request_context"]
