"""No anonymous routes (D21, H1).

The test CLAUDE.md says must never exempt a route: it enumerates every route on the public
application and asserts that a request without a token is refused. New routes are covered
the moment they are added, which is the point — a hand-maintained list would go stale, an
enumeration cannot.

Deliberately not an allowlist of convenience. There is exactly one exemption, it lives in
`app.ANONYMOUS_PREFIXES` rather than here, and the reasoning for it sits beside it. Adding
a second is a change to D21 and needs J's approval; `test_app_structure` fails until the
assertion recording the current one is edited too, so it cannot happen quietly.

Note what this suite enumerates: `served_table`, not `route_table`. Static files are not
API routes and carry no `/v1` prefix, but they are reachable, and D21 is about what a
request can reach. Mounting the UI added a 200 that answered without a token while the
route count stayed at 21 — see `harness/routes.py` for the measurement.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from circuless_node.app import ANONYMOUS_PREFIXES, create_public_app
from circuless_node.settings import Settings

from .harness.routes import served_table

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
    assert served_table(create_public_app(settings)), (
        "the public app serves no routes, so every check below is vacuous"
    )


def test_no_route_is_reachable_without_a_token(settings: Settings) -> None:
    app = create_public_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    allowed_through: list[str] = []

    for method, path in served_table(app):
        if _exempt(path):
            continue

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
        + "\nEvery route on the public app requires a valid token (D21), apart from "
        f"{ANONYMOUS_PREFIXES}. Never add to that."
    )


def _exempt(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in ANONYMOUS_PREFIXES)


def test_the_exemption_matches_a_prefix_and_not_a_substring() -> None:
    """`"/ui" in path` would exempt any route whose path happens to contain it.

    Undetectable against today's routes — nothing else contains the string — which is
    why it is pinned against `_exempt` directly rather than through the app. The route
    that would make it matter has not been written yet, and by then the mistake would
    be months old.
    """
    assert _exempt("/ui")
    assert _exempt("/ui/")
    assert _exempt("/ui/assets/tokens.css")

    assert not _exempt("/uix")
    assert not _exempt("/v1/ui")
    assert not _exempt("/v1/t/alpha/resources/build-ui")


def test_the_exemption_does_not_swallow_the_gate(settings: Settings) -> None:
    """Guards the guard. The failure this whole module exists to prevent, one level up.

    `test_the_exemption_covers_the_ui_and_nothing_else` asks the app directly, so it
    passes whatever `_exempt` does — the app really does answer 401 on `/v1`. If
    `_exempt` ever matched everything, the loop above would iterate over nothing and
    report success, and that test would not notice. This one asserts on the set the
    loop actually probes. Confirmed by mutation: `_exempt` returning `True` leaves
    every other test in this suite green and fails only this one.
    """
    app = create_public_app(settings)
    # Pairs, not paths: pairs are what the loop iterates, and `/v1/t/{t}/resources`
    # is four different requests to authorise.
    checked = {entry for entry in served_table(app) if not _exempt(entry[1])}

    assert ("GET", "/v1/whoami") in checked
    assert ("GET", "/v1/tenants") in checked
    assert ("GET", "/.well-known/circuless-node") in checked
    assert ("GET", "/ui/") not in checked
    # 21 when this was written. A floor, not an equality: it should not need editing
    # when a route is added, only when most of them stop being probed.
    assert len(checked) >= 20, f"the gate is only probing {len(checked)} requests"


def test_the_exemption_covers_the_ui_and_nothing_else(settings: Settings) -> None:
    """The exemption is bounded, proved by exercising both sides of the boundary.

    Without the second half this suite would still pass if `_exempt` matched everything,
    which is the shape a prefix check fails in.
    """
    client = TestClient(create_public_app(settings), raise_server_exceptions=False)

    assert client.get("/ui/").status_code == 200, "the UI must load before anyone has a token"
    assert client.get("/v1/tenants").status_code == 401
    assert client.get("/v1/whoami").status_code == 401
    assert client.get("/.well-known/circuless-node").status_code == 401


def test_the_exempt_tree_is_actually_reachable(settings: Settings) -> None:
    """An exemption for a path the app does not serve is a stale entry widening the rule.

    The same check `UNVERSIONED_PATHS` gets, for the same reason: this one would outlive
    the UI being removed, and would then be permission granted to nothing, waiting for
    whatever is mounted there next.
    """
    client = TestClient(create_public_app(settings), raise_server_exceptions=False)
    for prefix in ANONYMOUS_PREFIXES:
        assert client.get(f"{prefix}/").status_code != 404, f"nothing is served under {prefix}"
