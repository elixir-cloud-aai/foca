"""Unit test for security.cors.py."""

import pytest
from flask import Flask
from pydantic import ValidationError

from foca.models.config import CORSConfig
from foca.security.cors import enable_cors

ORIGIN = "https://example.org"


def _app(conf: CORSConfig | None = None) -> Flask:
    app = Flask(__name__)

    @app.route("/items", methods=["GET", "POST"])
    def items() -> str:
        return "ok"

    enable_cors(app, conf)
    return app


def test_default_any_origin() -> None:
    """Requests from any origin are allowed by default."""
    client = _app().test_client()
    for origin in [ORIGIN, "https://other.org"]:
        response = client.get("/items", headers={"Origin": origin})
        # flask-cors reflects the request origin for `*`
        assert response.headers["Access-Control-Allow-Origin"] == origin
        assert "Access-Control-Allow-Credentials" not in response.headers


def test_configured_origin() -> None:
    """Only configured origins are allowed."""
    client = _app(
        CORSConfig(
            allow_origins=[ORIGIN],
            allow_methods=["GET"],
            allow_headers=["Authorization"],
            expose_headers=["X-Total"],
            allow_credentials=True,
            max_age=600,
        )
    ).test_client()
    response = client.get("/items", headers={"Origin": ORIGIN})
    assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert response.headers["Access-Control-Expose-Headers"] == "X-Total"
    response = client.get("/items", headers={"Origin": "https://evil.org"})
    assert "Access-Control-Allow-Origin" not in response.headers
    # preflight
    response = client.options(
        "/items",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert response.headers["Access-Control-Allow-Methods"] == "GET"
    assert response.headers["Access-Control-Allow-Headers"].lower() == "authorization"
    assert response.headers["Access-Control-Max-Age"] == "600"


@pytest.mark.parametrize("origins", [["*"], [ORIGIN, "*"]])
def test_credentials_any_origin(origins: list[str]) -> None:
    """Credentials must not be allowed for any origin."""
    with pytest.raises(ValidationError):
        CORSConfig(allow_origins=origins, allow_credentials=True)
