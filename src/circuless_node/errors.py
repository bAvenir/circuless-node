"""Reason codes and the error shape (H3).

One enum, no free text. An error carries a status code and a reason code and nothing else:
no stack traces, no internal paths, no framework versions (SR-3.1.4, 3.2.5).

The enum exists from the first commit because reason codes are part of the API contract —
tests assert on them, and the node's clients branch on them. Adding a code means adding a
member here, never inventing a string at the call site.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import Request
from fastapi.responses import JSONResponse

from .storage import PathNotAllowedError


class Reason(StrEnum):
    # Identity and token handling (N2, N3)
    INVALID_TOKEN = "invalid_token"  # nosec B105 — a reason code, not a credential
    NODE_PRINCIPAL_NOT_PERMITTED = "node_principal_not_permitted"
    AMBIGUOUS_ACTING_ORG = "ambiguous_acting_org"

    # Authorization (N6, N18)
    NO_AGREEMENT = "no_agreement"
    NOT_PERMITTED = "not_permitted"

    # Resource registry (N5)
    #: NFR9 — a licence from the controlled list is required before publishing.
    LICENCE_REQUIRED = "licence_required"
    #: D22 — a BVR-operated node refuses classification=sensitive.
    CLASSIFICATION_NOT_PERMITTED = "classification_not_permitted"

    # Requests and resources
    CONFLICT = "conflict"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    PATH_NOT_ALLOWED = "path_not_allowed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED = "unsupported"

    # Upstream services (N9)
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_ERROR = "upstream_error"

    INTERNAL_ERROR = "internal_error"


class NodeError(Exception):
    """Every deliberate refusal in the node raises one of these."""

    def __init__(self, status_code: int, reason: Reason, detail: str | None = None) -> None:
        super().__init__(reason.value)
        self.status_code = status_code
        self.reason = reason
        # Shown to the caller, so it must stay free of internals. Optional on purpose: the
        # reason code is the contract, and detail is only ever a hint.
        self.detail = detail


def install_error_handlers(app) -> None:  # noqa: ANN001  — FastAPI app
    @app.exception_handler(PathNotAllowedError)
    async def _path_not_allowed(_request: Request, exc: PathNotAllowedError) -> JSONResponse:
        """Path confinement refusals are a refusal, not a crash (H3, SR-3.2.3).

        `Storage.resolve` raises this, and until N8 nothing could reach it — so it had no
        handler, and a `..` in a path would have come back as 500 `internal_error` while
        `PATH_NOT_ALLOWED` sat unused in the enum. Handled here rather than at each call
        site so that `/data/{path}`, `/invoke/{path}` (N9) and uploads (N19) are all
        covered by construction.

        The message is safe to pass on: `resolve` raises on the shape of the path and
        never includes the resolved location, so this cannot disclose where the data
        directory is.
        """
        return JSONResponse(
            status_code=400,
            content={"reason": Reason.PATH_NOT_ALLOWED.value, "detail": str(exc)},
        )

    @app.exception_handler(NodeError)
    async def _node_error(_request: Request, exc: NodeError) -> JSONResponse:
        body: dict[str, str] = {"reason": exc.reason.value}
        if exc.detail:
            body["detail"] = exc.detail
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, _exc: Exception) -> JSONResponse:
        # Anything unexpected becomes one opaque code. A stack trace in a response tells an
        # attacker the framework, the file layout and often the query.
        return JSONResponse(status_code=500, content={"reason": Reason.INTERNAL_ERROR.value})
