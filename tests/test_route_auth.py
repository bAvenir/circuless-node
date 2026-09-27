"""No anonymous routes (D21, H1).

The test CLAUDE.md says must never exempt a route: it enumerates every route on the public
application and asserts that a request without a token is refused. New routes are covered
the moment they are added, which is the point — a hand-maintained list would go stale, an
enumeration cannot.

Deliberately not an allowlist. If a route ever genuinely must be anonymous, that is a
change to D21 and needs J's approval, not an exception here.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from circuless_node.app import create_public_app
from circuless_node.settings import Settings

from .harness.routes import route_table

# A body is irrelevant to "was I let in without a token?", but a route that validates its
# schema before checking auth would answer 422 and hide the real behaviour, so each
# non-GET gets something plausible to chew on.
SAFE_BODY = {"probe": "route-auth"}


def test_the_public_app_serves_at_least_one_route(settings: Settings) -> None:
    """Guards the guard.

    While the public app had no routes, this suite was asserting things about an empty
    list and passing. Worse, the enumeration itself was broken — FastAPI wraps included
    routers, so filtering `app.routes` for APIRoute found nothing even after /v1/whoami
    existed. Both failures look identical from the outside: green.
    """
    assert route_table(create_public_app(settings)), (
        "the public app serves no routes, so every check below is vacuous"
    )


def test_no_route_is_reachable_without_a_token(settings: Settings) -> None:
    app = create_public_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    allowed_through: list[str] = []

    for method, path in route_table(app):
        # Path parameters get a value valid for any type the node uses.
        concrete = path
        while "{" in concrete:
            start, end = concrete.index("{"), concrete.index("}")
            concrete = (
                concrete[:start] + "00000000-0000-0000-0000-000000000000" + concrete[end + 1 :]
            )

        response = client.request(method, concrete, json=None if method == "GET" else SAFE_BODY)
        if response.status_code != 401:
            allowed_through.append(f"{method} {path} -> {response.status_code}")

    assert allowed_through == [], (
        "these routes answered something other than 401 without a token:\n  "
        + "\n  ".join(allowed_through)
        + "\nEvery route on the public app requires a valid token (D21). Never exempt one."
    )
