"""Structural invariants of the two applications.

These are the properties D24 and R8 describe, expressed as tests so they hold for every
route added later rather than only for the routes that exist today. They are cheap and they
are the reason the `/v1` decision is "now or never" — after M1 a prefix change breaks every
client and every test at once.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from circuless_node.app import API_PREFIX, create_internal_app, create_public_app
from circuless_node.settings import Settings

from .harness.routes import route_paths, route_table

# Paths the design allows outside /v1 (CLAUDE.md invariant 1). All of them live on the
# internal application; none is reachable through the gateway.
INTERNAL_PATHS = {"/healthz", "/metrics", "/internal/authz"}


def test_every_public_route_is_under_v1(settings: Settings) -> None:
    routes = route_table(create_public_app(settings))
    assert routes, (
        "nothing to check — see test_route_auth.test_the_public_app_serves_at_least_one_route"
    )
    offenders = [path for _, path in routes if not path.startswith(API_PREFIX)]
    assert offenders == [], (
        f"routes outside {API_PREFIX}: {offenders}. Every Node API route carries the prefix "
        "(D24); the only exceptions are /.well-known/circuless-node and the internal app."
    )


def test_public_app_exposes_no_internal_paths(settings: Settings) -> None:
    """The gateway can reach this app, so an authorization oracle here would be public (R8)."""
    assert not (route_paths(create_public_app(settings)) & INTERNAL_PATHS)


def test_public_app_publishes_no_schema(settings: Settings) -> None:
    """No /docs, /redoc or /openapi.json on the gateway-facing app.

    An unauthenticated index of every route is worth a great deal to anyone scanning, and
    the schema is published through the catalogue instead.
    """
    client = TestClient(create_public_app(settings))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, f"{path} is reachable on the public app"


def test_internal_app_serves_health_and_metrics(settings: Settings) -> None:
    client = TestClient(create_internal_app(settings))

    health = client.get("/healthz")
    assert health.status_code == 200
    # Status code only: a body would leak version and sync state to anything on the socket.
    assert health.content == b""

    assert client.get("/metrics").status_code == 200


def test_internal_app_keeps_its_docs(settings: Settings) -> None:
    """Swagger is useful to an operator on the overlay, and only to them."""
    assert TestClient(create_internal_app(settings)).get("/docs").status_code == 200


def test_cors_rejects_a_wildcard_origin() -> None:
    """A wildcard would let any site spend a user's node token (G7)."""
    with pytest.raises(ValueError, match=r"never '\*'"):
        Settings(node_id="test-node", cors_allow_origins=["*"])  # type: ignore[call-arg]


def test_cors_headers_are_returned_for_an_allowed_origin(settings: Settings) -> None:
    client = TestClient(create_public_app(settings))
    response = client.options(
        f"{API_PREFIX}/anything",
        headers={
            "Origin": "https://ui.circuless.eu",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("access-control-allow-origin") == "https://ui.circuless.eu"
    # Browser downloads through the gateway send Authorization cross-origin.
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_cors_ignores_an_unlisted_origin(settings: Settings) -> None:
    client = TestClient(create_public_app(settings))
    response = client.options(
        f"{API_PREFIX}/anything",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in response.headers


def test_audience_is_derived_from_the_node_id(settings: Settings) -> None:
    """N2 accepts exactly this audience and nothing else."""
    assert settings.audience == "node:test-node"
