"""The route enumerator itself (`tests/harness/routes.py`).

Testing a test helper is usually not worth it. This one is, because everything it feeds
is a security gate — the no-anonymous-route test (D21) and the `/v1` prefix test both
assert things about whatever this returns, and if it returns less than the truth they
pass by checking a shorter list.

That is not hypothetical. Three ways of writing this have silently under-reported across
the two CIRCULess repositories, and in one case the structural tests ran for days against
an empty list. Each failure is pinned here, against a purpose-built app rather than
against the node's own — so these keep working whatever the node's routes become, and so
a hidden or nested route exists to be found even while the node has none of its own.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from .harness.routes import route_paths, route_table


def app_with_every_awkward_shape() -> FastAPI:
    """One app containing each thing that has defeated a previous enumerator."""
    app = FastAPI()

    @app.get("/plain")
    def plain() -> dict:
        return {}

    # Hidden: absent from app.openapi() entirely.
    @app.get("/hidden", include_in_schema=False)
    def hidden() -> dict:
        return {}

    # Included router: app.routes holds a wrapper, not these.
    included = APIRouter()

    @included.get("/included")
    def in_router() -> dict:
        return {}

    @included.get("/included-hidden", include_in_schema=False)
    def hidden_in_router() -> dict:
        return {}

    # Nested under a prefix: these routes do not carry /v1 in their own `.path`.
    prefixed = APIRouter(prefix="/v1")
    nested = APIRouter()

    @nested.get("/nested")
    def in_nested() -> dict:
        return {}

    @prefixed.get("/direct")
    def direct_on_prefixed() -> dict:
        return {}

    prefixed.include_router(nested)
    app.include_router(included)
    app.include_router(prefixed)
    return app


def test_it_finds_routes_inside_an_included_router() -> None:
    """The first failure: `[r for r in app.routes if isinstance(r, APIRoute)]`.

    FastAPI wraps an included router, so that expression returns nothing the moment a
    router is used — which is how the N1 structural tests here passed by checking an
    empty list.
    """
    assert "/included" in route_paths(app_with_every_awkward_shape())


def test_it_finds_a_route_that_is_not_in_the_schema() -> None:
    """The second failure, and the reason this module changed.

    `app.openapi()` omits `include_in_schema=False` entirely. The node has no such route
    today, so nothing was being missed — but the gate would have stopped covering the
    first one silently, and every test would still have passed.
    """
    paths = route_paths(app_with_every_awkward_shape())
    assert "/hidden" in paths
    assert "/included-hidden" in paths


def test_it_reports_the_path_actually_served() -> None:
    """The third failure: a nested router's routes do not carry the parent's prefix.

    Reporting `/nested` for a route served at `/v1/nested` makes the route-auth test
    request a path that does not exist and read the 404 as "not 401" — a gate that
    checks nothing while looking busy.
    """
    paths = route_paths(app_with_every_awkward_shape())
    assert "/v1/nested" in paths
    assert "/nested" not in paths


def test_a_prefixed_routers_own_routes_are_not_double_prefixed() -> None:
    """The obvious fix for the third failure, applied one level too eagerly, yields
    `/v1/v1/direct`. Pinned because it looks right until you read the output."""
    paths = route_paths(app_with_every_awkward_shape())
    assert "/v1/direct" in paths
    assert "/v1/v1/direct" not in paths


def test_it_reports_every_route_exactly_once() -> None:
    table = route_table(app_with_every_awkward_shape())
    assert len(table) == len(set(table))


def test_it_omits_the_methods_nobody_authorises() -> None:
    """HEAD and OPTIONS are added by the framework and by CORS, not by us.

    Including them would make the route-auth test assert 401 on an OPTIONS preflight,
    which a browser sends without credentials by design.
    """
    methods = {method for method, _ in route_table(app_with_every_awkward_shape())}
    assert methods == {"GET"}
