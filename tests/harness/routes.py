"""Enumerating the routes an application actually serves.

Worth its own module because the obvious way is wrong, silently.

`[r for r in app.routes if isinstance(r, APIRoute)]` returns **nothing** once a router has
been included: FastAPI wraps it in a `_IncludedRouter`, so `app.routes` holds the wrapper
and not the routes inside it. Any test built on that pattern passes by checking an empty
list — which is exactly what happened to the N1 structural tests until `/v1/whoami` was
added and the count stayed at zero.

`app.openapi()` is FastAPI's own account of what it serves, works however routers are
nested, and keeps working if the nesting changes. It is available even when `openapi_url`
is `None`; that setting controls whether the schema is *served*, not whether it exists.
"""

from __future__ import annotations

HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


def route_table(app) -> list[tuple[str, str]]:  # noqa: ANN001  — a FastAPI app
    """Every (METHOD, path) pair the app serves, sorted."""
    paths = app.openapi().get("paths", {})
    return sorted(
        (method.upper(), path)
        for path, operations in paths.items()
        for method in operations
        if method.lower() in HTTP_METHODS
    )


def route_paths(app) -> set[str]:  # noqa: ANN001  — a FastAPI app
    return {path for _, path in route_table(app)}
