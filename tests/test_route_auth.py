"""No anonymous routes (D21, H1).

This is the test CLAUDE.md says must never exempt a route: it enumerates every route on the
public application and asserts that a request without a token is refused. New routes are
covered the moment they are added, which is the point — a list of routes to check would
grow stale, an enumeration cannot.

It is deliberately not an allowlist. If a route ever genuinely must be anonymous, that is a
change to D21 and needs J's approval, not an exception here.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from starlette.testclient import TestClient

from circuless_node.app import create_public_app
from circuless_node.settings import Settings

# A body is irrelevant to the question "was I let in without a token?", but a route that
# rejects on schema before checking auth would answer 422 and hide the real behaviour, so
# each method gets something minimally plausible to chew on.
SAFE_BODY = {"probe": "route-auth"}


def public_routes(settings: Settings) -> list[APIRoute]:
    return [r for r in create_public_app(settings).routes if isinstance(r, APIRoute)]


def route_cases(settings: Settings) -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for route in public_routes(settings):
        # Path parameters get a value that is valid for any of the types we use.
        path = route.path
        for name in route.param_convertors:
            path = path.replace(f"{{{name}}}", "00000000-0000-0000-0000-000000000000")
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            cases.append((method, path))
    return cases


def test_there_is_something_to_check(settings: Settings) -> None:
    """Guard against the suite passing because it found no routes.

    Until N2 mounts the first authenticated route there are none, and a vacuous pass here
    would be indistinguishable from a real one. This test is expected to fail until then,
    and is skipped rather than lying about it.
    """
    if not public_routes(settings):
        pytest.skip("no public routes yet — N2 onwards mount them under /v1")


def test_no_route_is_reachable_without_a_token(settings: Settings) -> None:
    cases = route_cases(settings)
    if not cases:
        pytest.skip("no public routes yet — N2 onwards mount them under /v1")

    client = TestClient(create_public_app(settings), raise_server_exceptions=False)
    allowed_through: list[str] = []

    for method, path in cases:
        response = client.request(method, path, json=SAFE_BODY if method != "GET" else None)
        if response.status_code != 401:
            allowed_through.append(f"{method} {path} -> {response.status_code}")

    assert allowed_through == [], (
        "these routes answered something other than 401 without a token:\n  "
        + "\n  ".join(allowed_through)
        + "\nEvery route on the public app requires a valid token (D21). Never exempt one."
    )
