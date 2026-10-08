"""Calling a partner's service on a consumer's behalf (N9, invariant 10, F9).

```
ANY /v1/t/{tenant}/resources/{id}/invoke/{path}
```

The other half of `invoke`, and the only place the node makes an outbound request on
somebody else's say-so. Authorised by `decide()` (N6) exactly like a download — an
agreement that permits `invoke` is what gets you here — and then the node, not the
caller, decides what the upstream sees.

## The request the upstream receives is built, not forwarded

That distinction is invariant 10 and it is the whole component. A proxy that passes a
request along is a proxy that passes along whatever the caller put in it; this one
starts from nothing and adds what it can justify.

**Stripped** — `Authorization`, `Cookie`, `Host`, hop-by-hop headers, and **every
inbound `X-CIRCULess-*`**.

**Injected** — the upstream credential (N10), plus `X-CIRCULess-Subject`, `-Org`,
`-Resource`, `-Request-Id` and `-Actor`.

**Forwarded** — `Content-Type`, `Accept`, `Content-Length` and `Idempotency-Key`; and
`Mcp-Session-Id`, `Mcp-Protocol-Version`, `Last-Event-ID` only when the resource is
`streaming`.

Stripping inbound `X-CIRCULess-*` is the one that matters most. Those headers are how
the node tells the upstream who is calling, and the upstream has no way to tell the
node's word from the caller's. Without the strip, a consumer sends
`X-CIRCULess-Org: someone-else` and the partner's service believes it.

`Authorization` is stripped for the mirror reason: the caller's node token is for this
node, and handing it to a partner's service would let that service replay it here.

## The response is an allowlist too

The invariant only names `Location`, but what comes back needs a rule or the proxy
becomes a way to set headers on the node's origin. `Set-Cookie` is the one worth
naming: a partner's service setting a cookie through the node would be a
session-fixation and CSRF vector aimed at every *other* tenant's endpoints on the same
host. `Server` and `X-Powered-By` go because they only tell an attacker what the
upstream runs.

`Location` is rewritten to this node's own `/invoke` URL when it points inside the
upstream, and refused when it points anywhere else — a redirect to a third party would
take the consumer out from behind the node, past `decide()` and past the log.

## Where the node may be sent

`policy.check_upstream_url`, again at call time and not only at registration. That one
is a genuine server-side request forgery guard, and the reasoning is there.

## What it does not do

**It never retries.** `idempotent_methods` is a method allowlist in the beta (see
`InvokePolicy`), not a retry list, so a dropped connection is an honest `502`.

**It holds no job state.** An async service answers `202` and that passes straight
through, `Location` rewritten so the consumer polls back through the node — and so the
poll is decided and logged like any other call.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlmodel import Session, col, select

from . import access_log
from .auth import requested_acting_org, require_subject
from .credentials import load_or_create_fernet, render, unseal
from .decide import Action, decide
from .errors import NodeError, Reason
from .models import AgreementCache, Resource, ServiceCredential, Tenant
from .policy import InvokePolicy, check_upstream_url, parse_invoke_policy
from .resources import tenant_org
from .settings import Settings
from .storage import check_relative_path
from .subject import Subject
from .tenancy import tenant_by_slug, tenant_scope
from .vocabularies import ResourceStatus

#: Never relayed in either direction: they describe the connection, not the message,
#: and forwarding them corrupts the one the node actually has.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: From the caller to the upstream (invariant 10). Everything absent here is dropped,
#: including headers that look harmless: an allowlist that grows by exception is an
#: allowlist, and one that grows by sympathy is not.
FORWARD_REQUEST = frozenset({"content-type", "accept", "content-length", "idempotency-key"})

#: Added to that list when the resource is `streaming`. MCP sessions and SSE resumption
#: need them, and a non-streaming service has no business seeing them.
FORWARD_REQUEST_STREAMING = frozenset({"mcp-session-id", "mcp-protocol-version", "last-event-id"})

#: From the upstream back to the caller.
FORWARD_RESPONSE = frozenset(
    {
        "content-type",
        "content-length",
        "content-disposition",
        "cache-control",
        "etag",
        "last-modified",
        "retry-after",
    }
)
FORWARD_RESPONSE_STREAMING = frozenset({"mcp-session-id"})

#: The node's own prefix. Stripped on the way in so a caller cannot forge one, and
#: injected on the way out so the upstream learns who is calling.
CIRCULESS_PREFIX = "x-circuless-"


def proxy_router() -> APIRouter:
    router = APIRouter()

    @router.api_route(
        "/t/{tenant_slug}/resources/{resource_id}/invoke/{upstream_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def invoke(
        request: Request,
        tenant_slug: str,
        resource_id: uuid.UUID,
        upstream_path: str,
        subject: Subject = Depends(require_subject),
        requested_org: str | None = Depends(requested_acting_org),
    ) -> StreamingResponse:
        decided = _authorised(request, subject, tenant_slug, resource_id, requested_org)
        return await _relay(request, subject, decided, upstream_path)

    return router


class _Decided:
    """What the authorisation step resolved, carried into the relay."""

    __slots__ = (
        "tenant_id",
        "tenant_slug",
        "resource",
        "policy",
        "acting_org",
        "entry_id",
        "credential",
    )

    def __init__(  # noqa: ANN001
        self, tenant_id, tenant_slug, resource, policy, acting_org, entry_id, credential
    ):
        self.tenant_id = tenant_id
        self.tenant_slug = tenant_slug
        self.resource = resource
        self.policy = policy
        self.acting_org = acting_org
        self.entry_id = entry_id
        self.credential = credential


def _authorised(
    request: Request,
    subject: Subject,
    tenant_slug: str,
    resource_id: uuid.UUID,
    requested_org: str | None,
) -> _Decided:
    """Resolve, decide, log — the same order as a download, for the same reasons.

    Nothing below the decision can be learned without permission: not whether the
    service exists, not what it permits, not whether it has a credential.
    """
    with Session(request.app.state.engine) as session:
        tenant: Tenant = tenant_by_slug(session, tenant_slug)
        owner = tenant_org(tenant)

        with tenant_scope(session, tenant.id):
            resource = session.get(Resource, resource_id)
        if resource is None:
            raise NodeError(404, Reason.NOT_FOUND, "no such resource")

        agreements = session.exec(
            select(AgreementCache).where(col(AgreementCache.provider_org) == owner)
        ).all()
        decision = decide(
            subject, Action.INVOKE, resource, owner, agreements, datetime.now(UTC), requested_org
        )

        entry_id = access_log.record(
            request.app.state.engine,
            tenant_id=tenant.id,
            request_id=access_log.request_id_of(request),
            action=Action.INVOKE.value,
            subject=subject,
            allowed=decision.allowed,
            reason=decision.reason,
            resource_id=resource.id,
            acting_org=decision.acting_org,
        )
        if not decision.allowed:
            status = 404 if decision.reason is Reason.NOT_FOUND else 403
            raise NodeError(status, decision.reason or Reason.NOT_PERMITTED, decision.detail)

        # Everything from here is a refusal to somebody already allowed.
        if resource.status is not ResourceStatus.ACTIVE or not resource.endpoint_url:
            raise NodeError(404, Reason.NOT_FOUND, "this service has no endpoint")

        policy = parse_invoke_policy(resource.invoke_policy)
        if not policy.permits(request.method):
            raise NodeError(
                405,
                Reason.UNSUPPORTED,
                f"this service does not permit {request.method}",
            )

        with tenant_scope(session, tenant.id):
            stored = session.get(ServiceCredential, resource.id)
            credential = None
            if stored is not None:
                fernet = load_or_create_fernet(request.app.state.settings)
                credential = render(stored, unseal(fernet, stored.secret))

        session.expunge_all()
        return _Decided(
            tenant.id, tenant.slug, resource, policy, decision.acting_org, entry_id, credential
        )


# --- building the upstream request ------------------------------------------------------


def upstream_request_headers(
    request: Request,
    decided: _Decided,
    subject: Subject,
    request_id: str,
) -> dict[str, str]:
    """Exactly what the upstream will see, built from nothing (invariant 10)."""
    allowed = FORWARD_REQUEST | (
        FORWARD_REQUEST_STREAMING if decided.policy.streaming else frozenset()
    )

    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lowered = name.lower()
        # The strip list is implicit: anything not on the allowlist is gone, which
        # covers Authorization, Cookie, Host, hop-by-hop and every inbound
        # X-CIRCULess-* without depending on a list of things to remember.
        if lowered in allowed and lowered not in HOP_BY_HOP:
            headers[lowered] = value

    headers[f"{CIRCULESS_PREFIX}subject"] = subject.sub
    headers[f"{CIRCULESS_PREFIX}resource"] = str(decided.resource.id)
    headers[f"{CIRCULESS_PREFIX}request-id"] = request_id
    if decided.acting_org:
        headers[f"{CIRCULESS_PREFIX}org"] = decided.acting_org
    if subject.actor:
        headers[f"{CIRCULESS_PREFIX}actor"] = subject.actor

    if decided.credential is not None:
        name, value = decided.credential
        headers[name] = value
    return headers


def upstream_url(endpoint_url: str, upstream_path: str, query: str) -> str:
    """Where the call goes. The path is confined before it is joined (H3)."""
    if upstream_path:
        check_relative_path(upstream_path)
    base = endpoint_url if endpoint_url.endswith("/") else endpoint_url + "/"
    target = urljoin(base, upstream_path) if upstream_path else base
    return f"{target}?{query}" if query else target


def rewrite_location(location: str, endpoint_url: str, node_invoke_base: str) -> str:
    """Point a redirect back through the node, or refuse it.

    A `Location` the consumer followed directly would take them out from behind the
    node — past `decide()`, past the agreement, past the log, and carrying whatever the
    upstream put in the URL. So a redirect inside the upstream is rewritten to the
    node's own `/invoke`, and one pointing anywhere else is refused rather than
    quietly passed on.
    """
    base = endpoint_url if endpoint_url.endswith("/") else endpoint_url + "/"
    absolute = urljoin(base, location)

    upstream = urlparse(base)
    target = urlparse(absolute)
    same_service = (target.scheme, target.hostname, target.port) == (
        upstream.scheme,
        upstream.hostname,
        upstream.port,
    ) and target.path.startswith(upstream.path)
    if not same_service:
        raise NodeError(
            502,
            Reason.UPSTREAM_ERROR,
            "the service redirected outside itself, which would take the caller out "
            "from behind this node",
        )

    relative = target.path[len(upstream.path) :].lstrip("/")
    rewritten = f"{node_invoke_base.rstrip('/')}/{relative}" if relative else node_invoke_base
    return f"{rewritten}?{target.query}" if target.query else rewritten


def response_headers(
    upstream: httpx.Response, decided: _Decided, node_invoke_base: str
) -> dict[str, str]:
    allowed = FORWARD_RESPONSE | (
        FORWARD_RESPONSE_STREAMING if decided.policy.streaming else frozenset()
    )
    headers: dict[str, str] = {}
    for name, value in upstream.headers.items():
        lowered = name.lower()
        if lowered in allowed and lowered not in HOP_BY_HOP:
            headers[lowered] = value

    location = upstream.headers.get("location")
    if location:
        headers["location"] = rewrite_location(
            location, decided.resource.endpoint_url or "", node_invoke_base
        )
    # Content-Length is recomputed by the server for a streamed body; keeping the
    # upstream's would contradict what we actually send.
    headers.pop("content-length", None)
    return headers


# --- the relay ------------------------------------------------------------------------------


async def _relay(
    request: Request, subject: Subject, decided: _Decided, upstream_path: str
) -> StreamingResponse:
    settings: Settings = request.app.state.settings
    request_id = access_log.request_id_of(request)

    endpoint = decided.resource.endpoint_url or ""
    # Again at call time, not only at registration: a hostname that resolved to a
    # partner's server yesterday can resolve to 127.0.0.1 today.
    check_upstream_url(endpoint, resolve=True)

    target = upstream_url(endpoint, upstream_path, request.url.query)
    headers = upstream_request_headers(request, decided, subject, request_id)
    body = await _read_body(request, decided.policy)

    # `follow_redirects=False` is load-bearing: `rewrite_location` below is what keeps a
    # consumer behind the node, and a client that followed the redirect itself would
    # have left before that ran.
    #
    # The transport is injectable and defaults to None, which is httpx's real one. It is
    # a test seam, and an unusual one to accept in production code — but a proxy's whole
    # contract is the request it *sends*, and the honest way to assert on that is to
    # intercept it. Standing up a real upstream instead is impossible here precisely
    # because `check_upstream_url` refuses loopback, which is the guard working.
    client = httpx.AsyncClient(
        timeout=decided.policy.deadline,
        follow_redirects=False,
        transport=getattr(request.app.state, "upstream_transport", None),
    )
    stream = client.stream(request.method, target, headers=headers, content=body)
    try:
        upstream = await stream.__aenter__()
    except httpx.TimeoutException:
        await client.aclose()
        raise NodeError(
            504, Reason.UPSTREAM_TIMEOUT, "the service did not answer in time"
        ) from None
    except httpx.HTTPError:
        await client.aclose()
        # No detail from the exception: it carries the upstream's address, which is the
        # provider's internal topology and not the consumer's business.
        raise NodeError(502, Reason.UPSTREAM_ERROR, "the service could not be reached") from None

    node_base = _invoke_base(settings, decided)
    try:
        out_headers = response_headers(upstream, decided, node_base)
    except NodeError:
        await stream.__aexit__(None, None, None)
        await client.aclose()
        raise

    # The request-id header is not set here: `install_request_id` puts it on every
    # response from this app, and setting it again produced the header twice.
    return StreamingResponse(
        _body(stream, client, upstream, request.app.state.engine, decided),
        status_code=upstream.status_code,
        headers=out_headers,
        # The upstream's own type, which `response_headers` already allowlisted.
        media_type=upstream.headers.get("content-type"),
    )


async def _body(stream, client: httpx.AsyncClient, upstream: httpx.Response, engine, decided):  # noqa: ANN001
    """Relay the bytes, then record how many.

    Unbuffered: each chunk goes out as it arrives, which is what makes SSE work at all.
    `record_bytes` is after the loop and not in a `finally`, the same as N8 — a consumer
    who disconnects leaves `bytes` null, which N11 defines as "granted, did not
    complete".
    """
    sent = 0
    try:
        # `aiter_bytes`, not `aiter_raw`. Raw would relay the upstream's bytes
        # untouched, which is what a proxy normally wants — but `Content-Encoding` is
        # not on the response allowlist, so a gzipped upstream would reach the consumer
        # as compressed bytes labelled as plain ones. Decoding here costs the node some
        # CPU and the consumer-side compression, and is correct; relaying raw would
        # need the encoding headers forwarded and `Content-Length` kept consistent,
        # which is a larger contract than the beta needs.
        async for chunk in upstream.aiter_bytes():
            sent += len(chunk)
            yield chunk
    finally:
        await stream.__aexit__(None, None, None)
        await client.aclose()
    access_log.record_bytes(engine, decided.entry_id, decided.tenant_id, sent)


async def _read_body(request: Request, policy: InvokePolicy) -> bytes | None:
    """The request body, refused past `max_request_bytes`.

    Buffered rather than streamed upstream, deliberately: the node has to count it to
    enforce the limit, and a service call is not a file upload — N19 streams, this does
    not. The limit is what keeps that honest.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    received = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        received += len(chunk)
        if received > policy.max_request_bytes:
            raise NodeError(
                413,
                Reason.PAYLOAD_TOO_LARGE,
                f"this service accepts at most {policy.max_request_bytes} bytes per call",
            )
        chunks.append(chunk)
    return b"".join(chunks) if chunks else None


def _invoke_base(settings: Settings, decided: _Decided) -> str:
    """This node's own address for the resource being invoked, for `Location`.

    The tenant's **slug**, not its id. `/v1/t/{tenant_slug}/...` is what the router
    matches and what `tenant_by_slug` looks up, so a URL built from the UUID is a 404 —
    a rewritten `Location` the consumer cannot follow.

    This was wrong until a test actually followed one. The unit test covering it
    compared the rewritten string against an expectation written from the same mistaken
    code, so the test agreed with the bug; the first request through a real server found
    it at once. `test_reference_service.py` follows it rather than comparing it.
    """
    base = (settings.gateway_base_url or settings.effective_overlay_base_url() or "").rstrip("/")
    return f"{base}/v1/t/{decided.tenant_slug}/resources/{decided.resource.id}/invoke"
