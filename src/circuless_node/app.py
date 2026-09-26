"""The two applications.

The node serves two sockets, not one:

  * the **public** app — everything under `/v1`, reachable through the gateway and over
    the overlay;
  * the **internal** app — `/healthz`, `/metrics`, `/internal/authz` and the API docs,
    bound to loopback or the overlay address.

They are separate ASGI applications rather than one app with a guard, because R8 asks for
these endpoints to be unreachable through the gateway, and an endpoint that is not on the
socket cannot be reached by forging a header. `/internal/authz` in particular is an
authorization oracle: anyone who can call it can ask "may X read Y?" as often as they like.

Everything on the public app lives under `/v1` (D24). That is enforced by construction — the
public app mounts one router, which carries the prefix — and asserted by a test, because
"now or never" applies: after M1 a prefix change breaks every client and every test.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from .db import create_db_engine
from .errors import install_error_handlers
from .settings import Settings, get_settings
from .storage import Storage

API_PREFIX = "/v1"


def _attach_resources(app: FastAPI, settings: Settings) -> None:
    app.state.settings = settings
    app.state.engine = create_db_engine(settings)
    app.state.storage = Storage(settings)


def create_public_app(settings: Settings | None = None) -> FastAPI:
    """Gateway- and overlay-facing. Every route here requires a token (D21)."""
    settings = settings or get_settings()
    app = FastAPI(
        title="CIRCULess Node",
        version="0.1.0",
        # No docs on the public app: the schema is published through the catalogue, and an
        # unauthenticated endpoint listing every route is a gift to anyone scanning.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    _attach_resources(app, settings)
    install_error_handlers(app)

    if settings.cors_allow_origins:
        # Browser download through the gateway is cross-origin and sends Authorization,
        # so the origin list is exact and credentials are allowed (G7). Settings reject '*'.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-CIRCULess-Acting-Org", "Range"],
            expose_headers=["Content-Range", "Accept-Ranges", "Location"],
        )

    app.include_router(v1_router())
    return app


def v1_router() -> APIRouter:
    """The only router on the public app.

    Empty at N1. Resources (N5), data (N8, N19) and invoke (N9) mount here, and each
    inherits the `/v1` prefix by construction rather than by remembering it.
    """
    return APIRouter(prefix=API_PREFIX)


def create_internal_app(settings: Settings | None = None) -> FastAPI:
    """Loopback or overlay only. Never routed by the gateway (C3)."""
    settings = settings or get_settings()
    app = FastAPI(
        title="CIRCULess Node (internal)",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    _attach_resources(app, settings)
    install_error_handlers(app)

    @app.get("/healthz", status_code=200)
    def healthz() -> Response:
        """Liveness. Status code only, no body (D21).

        A body would leak versions and sync state to anything that can reach the socket.
        N7 adds stale-cache signalling, which is reported through the status code.
        """
        return Response(status_code=200)

    @app.get("/metrics")
    def metrics() -> Response:
        # Prometheus text format arrives with C13; the route exists now so that the
        # interface split is settled and testable from the start.
        return Response(status_code=200, content="", media_type="text/plain; version=0.0.4")

    return app
