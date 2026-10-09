"""Enumerating the routes an application actually serves.

Its own module because every obvious way of doing this is wrong, and all of them fail
*silently* — a gate that quietly checks nothing looks exactly like a gate that passes.
Three have now been found across the two CIRCULess repositories, in this order:

**`app.routes` filtered for `APIRoute` misses included routers.** FastAPI wraps an
included router in a `_IncludedRouter`, so `app.routes` holds the wrapper and not the
routes inside it. That is what happened here: the N1 structural tests passed by checking
an empty list until `/v1/whoami` was added and the count stayed at zero.

**`app.openapi()` misses hidden routes.** A route declared `include_in_schema=False` does
not appear in the schema at all. This module used `openapi()` until now, and the node has
no such routes today — so nothing was being missed yet. That is the whole problem with it:
the day someone adds `include_in_schema=False` to a route, the no-anonymous-route gate
(D21) stops covering it and every test still passes. Found in `circuless-cloud`, where
`/healthz` is declared exactly that way.

**Descending into a nested router loses the prefix.** This one is a hazard of the *fix*
rather than of what it replaced: `openapi()` reports fully resolved paths and got this
right. Walking route objects does not — a router's own routes carry its prefix in
`route.path`, while the routes of a router included *into* it do not, because the prefix
is applied when a request is matched rather than when the route is declared. Walking
naively reports `/orgs` for a route served at `/v1/orgs`, so the route-auth test requests
a path that does not exist and reads the resulting 404 as "not 401". Found in
`circuless-cloud` when the registry router was mounted, which is why the prefix
accumulates below and why `test_route_enumeration.py` pins both directions of it.

**A plain Starlette `Route` is skipped entirely.** Found while fixing the fourth, by a
test that expected exactly one mount and got four: FastAPI's own `/docs`, `/redoc` and
`/openapi.json` are `Route`s, not `APIRoute`s, and so is anything added with
`app.add_route` or appended to `app.router.routes`. Measured: a `Route` serving 200
anonymously, with `route_table` returning `[]`. Harmless on the node today — the public
app disables the docs — which is exactly the condition under which a gap goes unnoticed.

**A mounted ASGI application that is not a router is skipped entirely.** The fourth, and
the one that prompted this paragraph. `_walk` descends into a mount only when the mounted
app has `.routes`; `StaticFiles` is a bare ASGI app and has none, so mounting the admin UI
at `/ui` added zero entries to the table and the no-anonymous-route gate never requested
the path. Measured before it was fixed: 21 routes before the mount, 21 after, and
`GET /ui/` answering 200 with no token.

So this walks the route objects, descends into wrappers, and accumulates the prefix. It
sees hidden routes, which the schema cannot, and it reports the path actually served.

Two functions come out of it, and the distinction is load-bearing. `route_table` is the
**API** surface — what invariant 1 means by a route that must carry `/v1`. `served_table`
is everything a request can **reach**, mounts included; static files are not API routes,
but they are reachable, and reachability is what D21 is about.
"""

from __future__ import annotations

from starlette.routing import Mount, Route

HTTP_METHODS = {"GET", "PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"}


def _walk(router, prefix: str = "") -> list[tuple[str, str]]:  # noqa: ANN001
    """Every (METHOD, path) under `router`, with `prefix` prepended.

    Takes the router rather than its route list, because the prefix to add comes from the
    **parent**: a router's own `APIRoute`s already have its prefix baked into `.path`,
    while the routes of a router included into it do not. Walking the list alone cannot
    tell the two apart, and adding the child's own prefix yields `/v1/v1/whoami`.
    """
    prefixed = prefix + getattr(router, "prefix", "")
    found: list[tuple[str, str]] = []

    for route in getattr(router, "routes", []):
        # `Route` covers both: `APIRoute` is a subclass of it, and the plain form is what
        # FastAPI's docs endpoints and `app.add_route` produce. Matching only `APIRoute`
        # left those unenumerated and therefore unguarded.
        if isinstance(route, Route):
            # Already carries this router's prefix; only the inherited one is missing.
            for method in (route.methods or set()) & HTTP_METHODS:
                found.append((method, prefix + route.path))
            continue

        inner = getattr(route, "original_router", None)
        if inner is not None:
            found.extend(_walk(inner, prefixed))
            continue

        # A Mount or a sub-application: `path` is where it is mounted.
        mounted = getattr(route, "app", None)
        if mounted is not None and hasattr(mounted, "routes"):
            found.extend(_walk(mounted, prefixed + getattr(route, "path", "")))
    return found


def route_table(app) -> list[tuple[str, str]]:  # noqa: ANN001  — a FastAPI app
    """Every (METHOD, path) pair the app serves, sorted, including hidden ones."""
    return sorted(
        {(method, path) for method, path in _walk(app) if method not in {"HEAD", "OPTIONS"}}
    )


def route_paths(app) -> set[str]:  # noqa: ANN001  — a FastAPI app
    return {path for _, path in route_table(app)}


def _walk_mounts(router, prefix: str = "") -> list[str]:  # noqa: ANN001
    """Where every non-router ASGI application is mounted, with `prefix` prepended."""
    prefixed = prefix + getattr(router, "prefix", "")
    found: list[str] = []

    for route in getattr(router, "routes", []):
        if isinstance(route, Route):
            continue

        inner = getattr(route, "original_router", None)
        if inner is not None:
            found.extend(_walk_mounts(inner, prefixed))
            continue

        # `isinstance`, not `hasattr(route, "app")`. Starlette's plain `Route` carries an
        # `.app` too — it is the wrapped endpoint — so duck-typing here reported `/docs`,
        # `/openapi.json` and `/redoc` as mounts. Caught by a test that expected exactly
        # one entry; a test that merely checked `/ui` was present would have passed.
        if not isinstance(route, Mount):
            continue

        mounted = route.app
        if hasattr(mounted, "routes"):
            # A sub-application: its own routes are reported by `_walk`, so the mount
            # point itself is not a leaf and does not belong here.
            found.extend(_walk_mounts(mounted, prefixed + route.path))
            continue

        found.append(prefixed + route.path)

    return found


def mount_paths(app) -> list[str]:  # noqa: ANN001  — a FastAPI app
    """Where applications that serve no enumerable routes are mounted, e.g. `/ui`.

    These are invisible to `route_table` by construction — there is nothing to walk
    inside them — which is why they get their own function rather than being quietly
    absent from one.
    """
    return sorted(set(_walk_mounts(app)))


def served_table(app) -> list[tuple[str, str]]:  # noqa: ANN001  — a FastAPI app
    """Every (METHOD, path) a request can reach: API routes, plus one probe per mount.

    A mount serves a whole tree, so it is represented by the one path that stands for
    the tree: the mount point with a trailing slash, which is what a browser asks for
    and what `StaticFiles(html=True)` answers with `index.html`. Probing a deeper path
    would test a 404 rather than the gate.
    """
    return sorted(set(route_table(app)) | {("GET", f"{mount}/") for mount in mount_paths(app)})
