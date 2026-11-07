# Testing apps

FOCA comes with helpers for the tests of apps, in `foca.testing`, so that they
don't copy the same setup, and test authentication as it works in production.
Install them with `pip install foca[testing]`. Use them rather than Flask's
test client and request contexts, which go away in the ASGI release (see
[Preparing for the ASGI release](preparing-for-asgi.md#tests)).

## Pytest plugin

Enable the plugin by its name in your `conftest.py`:

```python
pytest_plugins = ["foca.testing.pytest_plugin"]
```

It has:

- **A network guard**, for all tests: `connect()` to other hosts than the local
  one and the test database fails, and settings of proxies are unset. Mark a test
  with `@pytest.mark.allow_network` to lift it. It catches accidental connections,
  but isn't a sandbox: it starts with each test, so fixtures of broader scope than
  a function aren't covered, nor are DNS lookups or subprocesses. For a replica set in
  `MONGODB_TEST_URI`, only the hosts it names are allowed, so name all members or
  use `directConnection=true`.
- **The fixture `mongodb`**, which makes FOCA use a MongoDB for tests, and returns
  a client of it: an empty in-memory MongoDB of mongomock for each test, or, with
  the environment variable `MONGODB_TEST_URI` (e.g., in CI), that MongoDB.
  Databases that a test writes to through this client or FOCA, and that weren't
  there before, are dropped afterwards, so its user needs the rights to list and
  drop databases.
  Others are kept, but not protected from the test, and may be seen by it. So use a
  throwaway MongoDB without other data, one for each worker of parallel tests, and
  drop what stays behind: databases of aborted runs, and those created only by
  rarer commands, such as `$out` or `renameCollection`. Settings of deployments
  (`MONGO_URI` etc.) are unset. In memory, closed clients keep working, unlike real
  ones, so use a real MongoDB for testing what happens after
  `reopen_mongodb()`.
- **The fixture `issuer`**: a local identity provider (see below).
- **The fixture `foca_client`**: a [test client](#test-client) of the app that
  the fixture `foca_app` gives. Define `foca_app` in your `conftest.py`;
  without it, `foca_client` fails with a `UsageError`:

    ```python
    import pytest

    from foca import Foca


    @pytest.fixture
    def foca_app(mongodb):
        return Foca(config_file="config.yaml").create_app()


    def test_list_pets(foca_client) -> None:
        assert foca_client.get("/pets").json() == []
    ```

The fixtures last for one test. A new identity provider takes a moment, for its
key: for many tests, keep one in a fixture of a wider scope with the context
manager below.

## Test client

`foca.testing.Client` sends requests to an app, without a server, through all
that serves requests (security, validation, CORS, errors). Its API is that of
[httpx](https://www.python-httpx.org/) and of Starlette's `TestClient`, so your
tests stay as they are when FOCA moves to ASGI. Use it as a context manager,
as on ASGI, where the client starts and stops the app (its lifespan); on
Connexion 2, there is nothing to start:

```python
from foca import Foca
from foca.testing import Client

app = Foca(config_file="config.yaml").create_app()
headers = {"Authorization": f"Bearer {token}"}
with Client(app) as client:
    response = client.post("/pets", json={"name": "Rex"}, headers=headers)
    assert response.status_code == 200
    pet = response.json()
    response = client.get("/pets", params={"tag": ["a", "b"], "limit": 10})
```

- `app` is what `Foca.create_app()` returns; a Flask app works too, in this
  release.
- `get()`, `head()`, `options()`, `delete()`, `post()`, `put()`, `patch()` and
  `request(method, url)` take `params` (query parameters: a mapping, with lists
  for repeated ones, pairs, or a query string; they replace parameters of the
  same name in the URL, as in httpx), `headers` (a mapping or pairs) and
  `follow_redirects`; requests with bodies also `json` (sent as compact UTF-8
  JSON), `content` (raw bytes or text) or `data` (form fields).
- Responses have `status_code`, `headers` (case-insensitive, `getlist()` for
  all values), `content` (bytes), `text` and `json()`, which parses the body
  whatever its media type.
- Redirects aren't followed unless you pass `follow_redirects=True`, to the
  client or a request.
- The client keeps no cookies between requests; send them as `Cookie` headers.

### Requests without HTTP

For unit tests of code that reads the request via `foca.context` and
`foca.security.auth`, e.g., helpers of controllers, `foca.testing.request_context`
sets up a request without sending it:

```python
from foca.context import current_request
from foca.security.auth import current_token_info
from foca.testing import request_context

with request_context(
    "/pets/1?view=full",
    "PUT",
    headers={"X-Request-ID": "abc"},
    token_info={"sub": "alice"},
    app=app,
):
    assert current_request().query["view"] == "full"
    assert current_token_info() == {"sub": "alice"}
```

It takes the path (with a query string, if any), method, headers and the token
info that Connexion would have validated, and as keywords the app (an empty one
by default), further `params`, the `root_path` of the app and the
`client_address`. Code in the block sees the app's configuration, but no route,
security check or controller runs.

## Tokens from a local identity provider

Rather than mocking token validation or setting `verify_audience: false`, let your
tests get tokens from `foca.testing.issuer`. It serves OIDC discovery, keys and
user info on a local port, so that FOCA validates tokens as in production. It
depends neither on pytest nor on a test client:

```python
import pytest

from foca import Foca
from foca.testing import Client
from foca.testing.issuer import Issuer


@pytest.fixture
def foca_app(issuer, mongodb, monkeypatch, tmp_path):
    # Your configuration, trusting the local identity provider
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(f"security:\n  auth:\n    issuers:\n      - {issuer.url}\n")
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(overlay))
    return Foca(config_file="config.yaml").create_app()


def test_audience_is_checked(foca_client: Client, issuer: Issuer) -> None:
    token = issuer.token(sub="alice", aud="my-app", groups=["admins"])
    response = foca_client.get("/pets", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200

    token = issuer.token(sub="alice", aud="another-app")
    response = foca_client.get("/pets", headers={"Authorization": f"Bearer {token}"})
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
