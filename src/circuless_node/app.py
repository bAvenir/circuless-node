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

from fastapi import APIRouter, Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .access_log import REQUEST_ID_HEADER, install_request_id
from .auth import TokenVerifier, requested_acting_org, require_subject
from .credentials import credential_router
from .db import create_db_engine
from .errors import NodeError, install_error_handlers
from .proxy import proxy_router
from .resources import resource_router
from .settings import Settings, get_settings
from .storage import Storage
from .subject import Subject, resolve_acting_org
from .sync import SyncState, metrics_text
from .tenants import tenant_router
from .transfer import transfer_router
from .upload import upload_router
from .well_known import node_document

API_PREFIX = "/v1"

#: Public paths allowed outside `/v1` (invariant 1). Exactly one, and it is asserted, so
#: adding to this set fails a test before it reaches a review.
#:
#: `.well-known` is an interoperability convention: a client finds it by knowing the
#: convention, and versioning it would mean nobody could. It still requires a token —
#: D21 has no anonymous routes, and this one least of all (see `well_known`).
UNVERSIONED_PATHS = {"/.well-known/circuless-node"}


def _attach_resources(app: FastAPI, settings: Settings) -> None:
    app.state.settings = settings
    # One SyncState per process, shared by the loop that writes it and /metrics that
    # reads it. Held in memory by decision: a restart resets it, so it reports the age
    # of this process rather than the age of the cache, which outlives it in the
    # database. The metric is named accordingly.
    app.state.sync_state = SyncState()
    app.state.engine = create_db_engine(settings)
    app.state.storage = Storage(settings)
    app.state.verifier = TokenVerifier(settings)


def create_public_app(settings: Settings | None = None) -> FastAPI:
    """Gateway- and overlay-facing. Every route here requires a token (D21)."""
    settings = settings or get_settings()
    app = FastAPI(
        title="CIRCULess Node",
        version=__version__,
        # No docs on the public app: the schema is published through the catalogue, and an
        # unauthenticated endpoint listing every route is a gift to anyone scanning.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    _attach_resources(app, settings)
    install_error_handlers(app)
    # Outermost, so every response carries the id — including the 401s and 403s, which
    # are the ones someone asking "what happened to my request" most often has in hand.
    install_request_id(app)

    if settings.cors_allow_origins:
        # Browser download through the gateway is cross-origin and sends Authorization,
        # so the origin list is exact and credentials are allowed (G7). Settings reject '*'.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-CIRCULess-Acting-Org", "Range"],
            # The request id too (N11): a browser that cannot read it cannot quote it
            # when reporting a failed download, which is the one thing it is for.
            expose_headers=[
                "Content-Range",
                "Accept-Ranges",
                "Content-Disposition",
                "Location",
                REQUEST_ID_HEADER,
            ],
        )

    @app.get("/.well-known/circuless-node")
    def well_known(_: Subject = Depends(require_subject)) -> dict:
        """What this node is, and how to reach it (N12).

        Outside `/v1` by convention and inside D21 by rule: the dependency is what makes
        an anonymous request 401, and the route-auth test asserts it along with every
        other route rather than trusting this line.
        """
        return node_document(app.state.settings)

    app.include_router(v1_router())
    return app


def v1_router() -> APIRouter:
    """The only router on the public app.

    Resources (N5), data (N8, N19) and invoke (N9) mount here, and each inherits the `/v1`
    prefix by construction rather than by remembering it.
    """
    router = APIRouter(prefix=API_PREFIX)

    @router.get("/whoami")
    def whoami(
        subject: Subject = Depends(require_subject),
        requested_org: str | None = Depends(requested_acting_org),
    ) -> dict:
        """What this node makes of your token — the resolved subject, as `decide()` sees it.

        Useful in three places: it is the M1 demo — a node accepting a user token,
        refusing a node token, refusing a token meant for another node; it gives someone
        installing a node from the guide a way to confirm it reads their tokens correctly;
        and it is how the route-auth test gets something real to check.

        It discloses nothing the caller did not bring with them: these are their own
        claims, normalised.
        """
        body: dict = {
            "sub": subject.sub,
            "principal_type": subject.principal_type.value,
            "actor": subject.actor,
            "org_ids": sorted(subject.org_ids),
            "admin_of": sorted(subject.admin_of),
        }

        # Resolved against the subject's own organisations, since no resource is in play
        # here. A real endpoint narrows the candidates to whoever could authorise that
        # particular request, which is where ambiguity usually disappears.
        try:
            body["acting_org"] = resolve_acting_org(subject, subject.org_ids, requested_org)
        except NodeError as refusal:
            body["acting_org"] = None
            body["acting_org_reason"] = refusal.reason.value

        return body

    router.include_router(tenant_router())
    router.include_router(resource_router())
    router.include_router(transfer_router())
    router.include_router(upload_router())
    router.include_router(credential_router())
    router.include_router(proxy_router())
    return router


def create_internal_app(settings: Settings | None = None) -> FastAPI:
    """Loopback or overlay only. Never routed by the gateway (C3)."""
    settings = settings or get_settings()
    app = FastAPI(
        title="CIRCULess Node (internal)",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    _attach_resources(app, settings)
    install_error_handlers(app)
    # On the internal app too. Nothing here writes an AccessLog entry — `/internal/authz`
    # must not (R7) — but an operator correlating a gateway trace with this node's logs
    # needs the same id on both sockets.
    install_request_id(app)

    @app.get("/healthz", status_code=200)
    def healthz() -> Response:
        """Liveness. Status code only, no body (D21).

        A body would leak versions and sync state to anything that can reach the socket.

        **200 even when the agreement cache is stale.** An earlier note here said N7
        would signal staleness through the status code; that would be wrong. A node
        enforcing from a stale cache is doing exactly what F16 designed it to do, and a
        503 would have an orchestrator remove it during the very Cloud outage the cache
        exists to survive — and remove every node at once, since they would all be stale
        together. Staleness is on `/metrics`, where it is an alert rather than an
        eviction.
        """
        return Response(status_code=200)

    @app.get("/metrics")
    def metrics() -> Response:
        """Sync staleness now (F16); the rest of the node's metrics with C13.

        Internal socket only. Cache age and failure counts tell anyone who can read them
        when this node is running degraded, which is operational detail and not a thing
        to publish through the gateway (R8).
        """
        return Response(
            status_code=200,
            content=metrics_text(app.state.sync_state),
            media_type="text/plain; version=0.0.4",
        )

    return app
