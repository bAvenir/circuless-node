"""The route enumerator itself (`tests/harness/routes.py`).

Testing a test helper is usually not worth it. This one is, because everything it feeds
is a security gate — the no-anonymous-route test (D21) and the `/v1` prefix test both
assert things about whatever this returns, and if it returns less than the truth they
pass by checking a shorter list.

That is not hypothetical. Five ways of writing this have silently under-reported across
the two CIRCULess repositories, and in one case the structural tests ran for days against
an empty list. Each failure is pinned here, against a purpose-built app rather than
against the node's own — so these keep working whatever the node's routes become, and so
a hidden or nested route exists to be found even while the node has none of its own.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from .harness.routes import mount_paths, route_paths, route_table, served_table


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


def app_with_a_static_mount(tmp_path) -> FastAPI:  # noqa: ANN001 — pytest's tmp_path
    """The fourth failure, isolated: an ASGI app with no routes to walk."""
    (tmp_path / "index.html").write_text("<h1>served</h1>")
    app = FastAPI()

    @app.get("/guarded")
    def guarded() -> dict:
        return {}

    app.mount("/ui", StaticFiles(directory=tmp_path, html=True), name="ui")
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


def test_a_static_mount_is_invisible_to_route_table(tmp_path) -> None:  # noqa: ANN001
    """Not a bug — the distinction the two functions exist to make.

    `StaticFiles` has no `.routes`, so there is nothing to enumerate inside it, and it
    is not an API route in any case. `route_table` is the API surface and must not grow
    an entry for it, or invariant 1 starts failing on a path that was never meant to
    carry `/v1`.
    """
    paths = route_paths(app_with_a_static_mount(tmp_path))
    assert "/guarded" in paths
    assert not [path for path in paths if path.startswith("/ui")]


def test_served_table_reports_the_static_mount(tmp_path) -> None:  # noqa: ANN001
    """The failure itself. Before this, mounting the UI added nothing to the table and
    the no-anonymous-route gate never requested the path — 21 routes before the mount,
    21 after, and `GET /ui/` answering 200 with no token."""
    app = app_with_a_static_mount(tmp_path)
    assert mount_paths(app) == ["/ui"]
    assert ("GET", "/ui/") in served_table(app)


def test_it_finds_a_plain_starlette_route() -> None:
    """The fifth failure, found while fixing the fourth.

    `APIRoute` is what a FastAPI decorator produces; `app.add_route`, an append to
    `app.router.routes`, and FastAPI's own docs endpoints produce the plain `Route`,
    which is its superclass. Matching the subclass left them unenumerated: measured at
    `route_table() == []` against an app answering 200 on `/sneaky` with no token.
    """

    async def leaky(request):  # noqa: ANN001, ANN202
        return PlainTextResponse("no token needed")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.router.routes.append(Route("/sneaky", leaky, methods=["GET"]))

    assert ("GET", "/sneaky") in route_table(app)
    assert TestClient(app).get("/sneaky").status_code == 200, (
        "the route must really be reachable, or this pins nothing"
    )


def test_it_finds_the_frameworks_own_docs_routes() -> None:
    """The same failure in the form it actually ships in.

    The node's public app disables these, so nothing was being missed there — and that
    is the condition under which the gap survives. The internal app serves them.
    """
    paths = route_paths(FastAPI())
    assert {"/docs", "/openapi.json", "/redoc"} <= paths


def test_a_websocket_route_is_not_reported_as_a_mount(tmp_path) -> None:  # noqa: ANN001
    """Why the mount check is `isinstance(Mount)` and not `hasattr(route, "app")`.

    `WebSocketRoute` carries an `.app` and is neither a `Route` nor a `Mount`, so
    duck-typing reports it as a mounted application — and `served_table` then probes it
    with a GET that cannot mean anything. The node has no websocket routes; this pins
    the reading rather than the current route list.
    """

    async def socket(websocket):  # noqa: ANN001, ANN202
        await websocket.close()

    app = app_with_a_static_mount(tmp_path)
    app.router.routes.append(WebSocketRoute("/ws", socket))

    assert mount_paths(app) == ["/ui"]


def test_served_table_probes_a_path_the_mount_actually_answers(tmp_path) -> None:  # noqa: ANN001
    """The trailing slash is load-bearing.

    `/ui` without it redirects, and a deeper path 404s. Either would make the gate read
    "not 401" from something other than the gate, which is how a check comes to pass for
    the wrong reason.
    """
    app = app_with_a_static_mount(tmp_path)
    [(_, probe)] = [entry for entry in served_table(app) if entry[1].startswith("/ui")]
    assert TestClient(app).get(probe).status_code == 200


def test_served_table_still_contains_every_api_route(tmp_path) -> None:  # noqa: ANN001
    """It adds to the table; it must never be a different, shorter one."""
    app = app_with_a_static_mount(tmp_path)
    assert set(route_table(app)) <= set(served_table(app))


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
