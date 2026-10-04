"""Every decision this node made (N11, invariant 12).

Allow and deny, consumption and management. The log is the node's half of the audit
story: the Cloud records who agreed to what (C18), and this records who then did what
with it. Neither can answer an incident on its own.

## It is written outside the request's transaction, on purpose

`record()` takes the **engine**, never a `Session`. That is not an inconvenience to work
around — it is the guarantee, expressed in the signature. A denial is almost always
followed by a raised `NodeError`, the handler's `with Session(...)` exits without
committing, and anything written through that session is rolled back. A log entry
sharing the transaction would therefore record every allow and silently lose every
deny — which is precisely backwards, since the denials are what an investigation is
usually about.

**This is the opposite of the Cloud's audit log, and deliberately so.** C18 writes inside
the caller's transaction because an entry there describes a *change*, and an entry
describing a change that was rolled back would be a lie. An entry here describes a
*decision*, which happened whatever became of the request afterwards.

The cost is a second connection per decision, and that a crash between the two leaves an
entry for a request that did nothing. Both are the right way round: an over-recorded
decision is an answerable question, an unrecorded refusal is not.

## bytes is the one mutable field, and the database enforces it

A transfer's size is only known once it has streamed, but the entry has to exist before
it starts — otherwise a connection dropped mid-stream would leave no trace that access
was granted at all. So `bytes` lands later, through `record_bytes()`, and the migration's
trigger allows exactly one null-to-value write and refuses everything else: a second
`bytes` write, any other column, any delete. Measured on SQLite before it was written,
not assumed.

## No names, no emails (D31, invariant 12)

`subject_sub` is Keycloak's UUID and nothing else. Node-audienced tokens carry no `name`
or `email` claim, so there is nothing here to accidentally copy — and the log stays
useful for an incident without becoming a directory of who works where.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session, col, desc, select

from .errors import Reason
from .models import AccessLog
from .subject import Subject
from .tenancy import tenant_scope

#: Echoed on every response from the public app, and injected upstream by the proxy
#: (N9, invariant 10) so a partner's service logs the same id this node did.
REQUEST_ID_HEADER = "X-CIRCULess-Request-Id"


def new_request_id() -> str:
    """A fresh id for this request.

    Always minted here, never read from an inbound header — not even to honour a
    caller's trace id. An id the caller chooses can be repeated, or collided with
    somebody else's, and the one thing this field has to be good for is finding every
    entry belonging to one request during an investigation of that caller.
    """
    return uuid.uuid4().hex


def install_request_id(app: Any) -> None:
    """Mint a request id for every request and return it on the response.

    Raw ASGI rather than `@app.middleware("http")`. The latter is Starlette's
    `BaseHTTPMiddleware`, which buffers through an anyio stream and has a long history of
    interfering with streaming responses — and N8 streams file bytes while N9 streams SSE
    unbuffered (invariant 10). Twenty lines here avoid finding that out later.
    """
    app.add_middleware(_RequestIdMiddleware)


class _RequestIdMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = new_request_id()
        # Starlette builds `request.state` from this dict, so handlers and dependencies
        # read it as `request.state.request_id`.
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_header(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
            await send(message)

        await self.app(scope, receive, send_with_header)


def request_id_of(request: Any) -> str:
    """The id the middleware minted for this request.

    Falls back to a fresh one rather than raising. A missing id means the middleware was
    not installed, which is a wiring bug — but failing the request over it would turn a
    logging fault into a denial of service, and the entry is still worth having even
    with an id that correlates with nothing.
    """
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    return new_request_id()


def record(
    engine: Engine,
    *,
    tenant_id: uuid.UUID,
    request_id: str,
    action: str,
    subject: Subject,
    allowed: bool,
    reason: Reason | None = None,
    resource_id: uuid.UUID | None = None,
    acting_org: str | None = None,
) -> uuid.UUID:
    """Write one decision and return its id.

    The id is only needed by transfers, which come back with `record_bytes()`; everything
    else can ignore it.

    `action` is a plain string because the two vocabularies that reach it —
    `decide.Action` and `ManagementAction` — are both `StrEnum`, and one column answering
    "what did they try to do" is worth more than two that each answer half.
    """
    entry = AccessLog(
        tenant_id=tenant_id,
        request_id=request_id,
        resource_id=resource_id,
        action=str(action),
        subject_sub=subject.sub,
        principal_type=subject.principal_type.value,
        actor_azp=subject.actor,
        acting_org=acting_org,
        decision="allow" if allowed else "deny",
        reason=reason.value if reason is not None else None,
    )
    # Its own session, its own transaction — see the module docstring. The engine is the
    # parameter precisely so that passing the caller's session is not possible.
    with Session(engine) as session, tenant_scope(session, tenant_id):
        session.add(entry)
        session.commit()
        return entry.id


def record_bytes(engine: Engine, entry_id: uuid.UUID, tenant_id: uuid.UUID, count: int) -> None:
    """Fill in how much was transferred. Once, and only for an entry that has none.

    Called after the body has streamed, so a transfer that failed halfway leaves `bytes`
    null — which reads as "access was granted and the transfer did not complete", and is
    a more honest record than a partial count presented as a total.

    The trigger would refuse a second call anyway; this checks first so that a bug here
    surfaces as a no-op rather than as a 500 on a request that otherwise succeeded. The
    trigger stays the guarantee, this is only the manners.
    """
    with Session(engine) as session, tenant_scope(session, tenant_id):
        entry = session.get(AccessLog, entry_id)
        if entry is None or entry.bytes is not None:
            return
        entry.bytes = count
        session.add(entry)
        session.commit()


def entries(
    session: Session,
    *,
    limit: int = 100,
    offset: int = 0,
    resource_id: uuid.UUID | None = None,
    decision: str | None = None,
) -> list[AccessLog]:
    """This tenant's log, newest first.

    No tenant filter here: the session is scoped (N4), and `AccessLog` is tenant-owned,
    so one is applied centrally. Writing a second one by hand is what invariant 8 forbids
    — and it would be the one that gets forgotten.
    """
    statement = select(AccessLog).order_by(desc(col(AccessLog.ts)), desc(col(AccessLog.id)))
    if resource_id is not None:
        statement = statement.where(col(AccessLog.resource_id) == resource_id)
    if decision is not None:
        statement = statement.where(col(AccessLog.decision) == decision)
    return list(session.exec(statement.offset(offset).limit(limit)).all())


def entry_out(entry: AccessLog) -> dict:
    return {
        "id": str(entry.id),
        "ts": entry.ts.isoformat(),
        "request_id": entry.request_id,
        "resource_id": str(entry.resource_id) if entry.resource_id else None,
        "action": entry.action,
        "subject_sub": entry.subject_sub,
        "principal_type": entry.principal_type,
        "actor": entry.actor_azp,
        "acting_org": entry.acting_org,
        "decision": entry.decision,
        "reason": entry.reason,
        "bytes": entry.bytes,
    }
