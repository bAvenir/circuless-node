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

So this walks the route objects, descends into wrappers, and accumulates the prefix. It
sees hidden routes, which the schema cannot, and it reports the path actually served.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

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
        if isinstance(route, APIRoute):
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
