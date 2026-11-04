# Testing apps

FOCA comes with helpers for the tests of apps, in `foca.testing`, so that they
don't copy the same setup, and test authentication as it works in production.
Install them with `pip install foca[testing]`.

## Pytest plugin

Enable the plugin by its name in your `conftest.py`:

```python
pytest_plugins = ["foca.testing.pytest_plugin"]
```

It has:

- **A network guard**, for all tests: `connect()` to other hosts than the local
  one and the test database fails, and settings of proxies are unset. Mark a test
  with `@pytest.mark.allow_network` to lift it. It catches accidental connections,
  but isn't a sandbox: it starts with each test, so session-scoped fixtures aren't
  covered, nor are DNS lookups or subprocesses. For a replica set in
  `MONGODB_TEST_URI`, only the hosts it names are allowed, so name all members or
  use `directConnection=true`.
- **The fixture `mongodb`**, which makes FOCA use a MongoDB for tests, and returns
  a client of it: an empty in-memory MongoDB of mongomock for each test, or, with
  the environment variable `MONGODB_TEST_URI` (e.g., in CI), that MongoDB.
  Databases that a test writes to through this client or FOCA, and that weren't
  there before, are dropped afterwards, so its user needs the right to do that.
  Others are kept, but not protected from the test, and may be seen by it. So use a
  throwaway MongoDB without other data, one for each worker of parallel tests, and
  drop what stays behind: databases of aborted runs, and those created only by
  rarer commands, such as `$out` or `renameCollection`. Settings of deployments
  (`MONGO_URI` etc.) are unset. In memory, closed clients keep working, unlike real
  ones, so use a real MongoDB for testing what happens after
  `reopen_mongodb()`.
- **The fixture `issuer`**: a local identity provider (see below).

The fixtures last for one test. A new identity provider takes a moment, for its
key: for many tests, keep one in a fixture of a wider scope with the context
manager below.

## Tokens from a local identity provider

Rather than mocking token validation or setting `verify_audience: false`, let your
tests get tokens from `foca.testing.issuer`. It serves OIDC discovery, keys and
user info on a local port, so that FOCA validates tokens as in production. It
depends neither on pytest nor on a test client:

```python
import pytest

from foca import Foca
from foca.testing.issuer import Issuer


@pytest.fixture
def client(issuer, mongodb, monkeypatch, tmp_path):
    # Your configuration, trusting the local identity provider
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(f"security:\n  auth:\n    issuers:\n      - {issuer.url}\n")
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(overlay))
    return Foca(config_file="config.yaml").create_app().app.test_client()


def test_audience_is_checked(client, issuer: Issuer) -> None:
    token = issuer.token(sub="alice", aud="my-app", groups=["admins"])
    response = client.get("/pets", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200

    token = issuer.token(sub="alice", aud="another-app")
    response = client.get("/pets", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
```

`tests/testing/test_end_to_end.py` runs this against the example app with access
control.

Outside pytest, use the context manager `with foca.testing.issuer.issuer() as
idp:`. `idp.url` is the identifier of the issuer, for `security.auth.issuers`.

`token()` takes `sub`, `aud`, `typ` (by default `at+jwt`), `groups`, `client_id`,
`expires_in` (negative for expired tokens) and further claims, which replace the
others. By default, responses for user info have the subject of the token.
`set_userinfo()` adds claims or sets another status code:

```python
issuer.set_userinfo(groups=["admins"])
issuer.set_userinfo(status=401)
```

## A local identity provider for development

FOCA ships no identity provider image. For trying out an app locally, a standard
mock OIDC server does the job, e.g., in `docker-compose.yaml`:

```yaml
services:
  oidc:
    # Pin by digest, as with all images
    image: ghcr.io/navikt/mock-oauth2-server:6.1.4@sha256:647dd8c78a54f0e6fcd58d79c884e612f8f998a0662276f3e1db22e464a50192
    ports:
      - "8090:8080"
```

Its issuer for the name `default` is `http://localhost:8090/default`. Set this in
`security.auth.issuers`, and get tokens from its login page. It's for development
only: it issues tokens to anyone. Check its tokens against your
[settings](configuration/auth.md), e.g., `audience` and `token_types`, as its
default claims and token type may need a `JSON_CONFIG` of the server or settings
for development.
