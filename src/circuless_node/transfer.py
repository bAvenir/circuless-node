"""Serving a dataset's bytes (N8, F5, F6).

The first endpoints where `decide()` (N6) actually runs and the AccessLog (N11) actually
fills — everything before this was registration and metadata.

```
GET /v1/t/{tenant}/resources/{id}/data          file bytes, or a bucket manifest
GET /v1/t/{tenant}/resources/{id}/data/{path}   one object of a bucket
```

## One order, and it is not negotiable

Resolve the tenant, load the resource, decide, **log**, and only then touch the
filesystem. Every refusal below the decision is therefore a refusal to somebody who was
already allowed, and nothing above it can be learned without permission — not whether a
file exists, not how big it is, not what a bucket contains.

The path is validated *after* the decision for the same reason: whether `../../etc` is a
legal path is not an authorisation question, and answering it first would tell an
unauthorised caller something about the path grammar.

## Withdrawn, missing, and the difference between them

A **withdrawn** resource is a decision: `decide()` denies it with `not_found` (D25), and
that denial is logged like any other. A resource that **never existed** is not a
decision — there is nothing to record it against, and logging it would let anyone fill a
tenant's log by guessing UUIDs. It is a bare 404.

Same rule as the unknown tenant in `resources._authorised_tenant`, and for the same
reason.

## No Range support in the beta

`Accept-Ranges: none`, always 200, always the whole file. Range was considered and
dropped: the single-range case is cheap but nothing in the acceptance set needs it, and
the deciding argument was that resumable downloads are an optimisation while every line
here is security-relevant surface. Adding it later is backwards compatible.

The one thing worth remembering if it is added: Range must be parsed **after** the
decision and the log write, because a `416` carries `Content-Range: bytes * /size` and
would otherwise disclose a file's size to someone with no agreement.

## What a transfer records

One AccessLog entry per request, written before any bytes move, with `bytes` filled in
once the stream finishes. A client that disconnects halfway leaves it null, which N11
defines as "access was granted and the transfer did not complete" — more honest than a
partial count presented as a total.

A bucket's objects are decided and logged **individually**. Not because objects can
differ — nothing in the model expresses per-object policy, so they cannot — but because
the log should show *which* of a 500-file campaign someone pulled, and because
agreements refresh every 30 s: a revocation takes effect mid-campaign rather than the
manifest acting as a bearer token for the whole bucket.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.parse
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from . import access_log
from .auth import requested_acting_org, require_subject
from .decide import Action, decide
from .errors import NodeError, Reason
from .models import AgreementCache, Resource, Tenant
from .resources import tenant_org
from .storage import Storage, check_relative_path
from .subject import Subject
from .tenancy import tenant_by_slug, tenant_scope
from .vocabularies import Shape

#: Read and yielded at a time. Large enough that a gigabyte file is not a million
#: syscalls, small enough that a few concurrent downloads do not hold tens of megabytes.
CHUNK_BYTES = 64 * 1024

#: Objects a manifest will list before saying it was cut short. Not a paging window —
#: see `Storage.list_objects`.
MANIFEST_LIMIT = 10_000

#: What a file is served as when nothing better is known. `Resource` carries no media
#: type in the beta, so it is guessed from the name and falls back to this; a real
#: `dcat:mediaType` field arrives in M4.
DEFAULT_MEDIA_TYPE = "application/octet-stream"


def transfer_router() -> APIRouter:
    router = APIRouter()

    @router.get("/t/{tenant_slug}/resources/{resource_id}/data")
    def read_data(
        request: Request,
        tenant_slug: str,
        resource_id: uuid.UUID,
        subject: Subject = Depends(require_subject),
        requested_org: str | None = Depends(requested_acting_org),
    ):
        """A file's bytes, or a bucket's manifest — whichever this resource is."""
        with Session(request.app.state.engine) as session:
            tenant, resource, entry_id = _authorised(
                request, session, subject, tenant_slug, resource_id, requested_org
            )

        storage: Storage = request.app.state.storage
        engine: Engine = request.app.state.engine

        if resource.shape is Shape.BUCKET:
            return _manifest(storage, engine, tenant, resource, entry_id)
        return _file(storage, engine, tenant, resource, entry_id)

    @router.get("/t/{tenant_slug}/resources/{resource_id}/data/{object_path:path}")
    def read_object(
        request: Request,
        tenant_slug: str,
        resource_id: uuid.UUID,
        object_path: str,
        subject: Subject = Depends(require_subject),
        requested_org: str | None = Depends(requested_acting_org),
    ):
        """One object of a bucket, decided and logged on its own."""
        with Session(request.app.state.engine) as session:
            tenant, resource, entry_id = _authorised(
                request, session, subject, tenant_slug, resource_id, requested_org
            )

        if resource.shape is not Shape.BUCKET:
            raise NodeError(
                400,
                Reason.UNSUPPORTED,
                "this resource is a single file; request /data without a path",
            )

        storage: Storage = request.app.state.storage
        # Checked *before* the join, not after. `PurePosixPath` collapses repeated
        # slashes, so joining first would turn `https://evil/x` into `https:/evil/x` and
        # the URL rule would never see what the caller actually sent — the resulting 404
        # would look like an ordinary missing file rather than a refused traversal.
        # `resolve` checks the joined path again; this is about refusing by name.
        check_relative_path(object_path)
        relative = str(PurePosixPath(resource.storage_path or "") / object_path)
        return _file(
            storage,
            request.app.state.engine,
            tenant,
            resource,
            entry_id,
            relative_path=relative,
            download_name=PurePosixPath(object_path).name,
        )

    return router


# --- authorisation, shared by both routes ---------------------------------------------


def _authorised(
    request: Request,
    session: Session,
    subject: Subject,
    tenant_slug: str,
    resource_id: uuid.UUID,
    requested_org: str | None,
) -> tuple[Tenant, Resource, uuid.UUID]:
    """Resolve, decide, log. Returns the entry id so the stream can fill in `bytes`.

    Deliberately not `resources._authorised_tenant`: that one answers "may you *manage*
    this tenant" through `decide_management` (N18). This is consumption, which is
    `decide()` (N6) — a different question with a different table, and merging them is
    how a management right would quietly become a consumption right.
    """
    tenant = tenant_by_slug(session, tenant_slug)
    owner = tenant_org(tenant)

    with tenant_scope(session, tenant.id):
        # Withdrawn resources are loaded on purpose — `decide()` owns that refusal, and a
        # handler filtering them out first would move a rule out of the one function
        # allowed to hold one.
        resource = session.get(Resource, resource_id)
    if resource is None:
        raise NodeError(404, Reason.NOT_FOUND, "no such resource")

    # Node-global (R10), so no tenant scope: an agreement belongs to neither tenant's
    # rows. Narrowed to this provider here rather than inside `decide()`, which is pure
    # and does not query.
    agreements = session.exec(
        select(AgreementCache).where(col(AgreementCache.provider_org) == owner)
    ).all()

    decision = decide(
        subject, Action.READ, resource, owner, agreements, datetime.now(UTC), requested_org
    )

    entry_id = access_log.record(
        request.app.state.engine,
        tenant_id=tenant.id,
        request_id=access_log.request_id_of(request),
        action=Action.READ.value,
        subject=subject,
        allowed=decision.allowed,
        reason=decision.reason,
        resource_id=resource.id,
        acting_org=decision.acting_org,
    )

    if not decision.allowed:
        # `not_found` keeps its own status: a withdrawn resource must not be
        # distinguishable from one that never existed (D25), and a 403 would announce
        # that something is there.
        status = 404 if decision.reason is Reason.NOT_FOUND else 403
        raise NodeError(status, decision.reason or Reason.NOT_PERMITTED, decision.detail)

    return tenant, resource, entry_id


# --- serving --------------------------------------------------------------------------


def _manifest(
    storage: Storage, engine: Engine, tenant: Tenant, resource: Resource, entry_id: uuid.UUID
) -> dict:
    """What is in this bucket, as paths that can be handed straight back to `/data/{path}`."""
    if not resource.storage_path:
        objects, truncated = [], False
    else:
        objects, truncated = storage.list_objects(tenant.id, resource.storage_path, MANIFEST_LIMIT)

    body = {
        "objects": [
            {"path": item.path, "size": item.size, "modified": item.modified.isoformat()}
            for item in objects
        ],
        "truncated": truncated,
    }
    # The manifest is the response, so its own size is what this request transferred.
    # One rule for the column — bytes sent — whatever the body happens to be.
    access_log.record_bytes(engine, entry_id, tenant.id, _json_size(body))
    return body


def _json_size(body: dict) -> int:
    return len(json.dumps(body).encode())


def _file(
    storage: Storage,
    engine: Engine,
    tenant: Tenant,
    resource: Resource,
    entry_id: uuid.UUID,
    relative_path: str | None = None,
    download_name: str | None = None,
) -> StreamingResponse:
    path = relative_path if relative_path is not None else resource.storage_path
    if not path:
        # Registered but never uploaded. The resource exists and the caller may read it;
        # there is simply nothing there yet. `not_found` with a detail saying so, because
        # to a consumer it is indistinguishable from a path that is not there — while the
        # owner debugging their own upload gets told which it is.
        raise NodeError(404, Reason.NOT_FOUND, "no data has been uploaded for this resource")
    if not storage.exists(tenant.id, path):
        raise NodeError(404, Reason.NOT_FOUND, "no data has been uploaded for this resource")

    name = download_name or PurePosixPath(path).name
    size = storage.size(tenant.id, path)

    return StreamingResponse(
        _stream(storage, engine, tenant.id, path, entry_id),
        media_type=mimetypes.guess_type(name)[0] or DEFAULT_MEDIA_TYPE,
        headers={
            "Content-Length": str(size),
            # No Range in the beta, said explicitly rather than by omission — a client
            # that reads this will not try to resume and get a silent full re-download.
            "Accept-Ranges": "none",
            "Content-Disposition": _disposition(name),
        },
    )


def _stream(
    storage: Storage, engine: Engine, tenant_id: uuid.UUID, path: str, entry_id: uuid.UUID
) -> Iterator[bytes]:
    """Yield the file, then record how much of it went.

    `record_bytes` is after the loop and deliberately not in a `finally`: if the client
    disconnects, the generator is closed, this line never runs, and `bytes` stays null —
    which is exactly what N11 means by "granted, did not complete".
    """
    sent = 0
    with storage.open(tenant_id, path, "rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            sent += len(chunk)
            yield chunk
    access_log.record_bytes(engine, entry_id, tenant_id, sent)


def _disposition(name: str) -> str:
    """`Content-Disposition`, with the filename encoded rather than trusted.

    `storage_path` is provider-supplied and only length-limited, so a basename could
    carry quotes, newlines or non-ASCII. The quoted form is stripped to a conservative
    set for old clients, and `filename*` (RFC 5987) carries the real name percent-encoded
    — which is also what stops a CR or LF from ever reaching a header value.
    """
    safe = "".join(character for character in name if character.isalnum() or character in "._- ")
    safe = safe.strip() or "download"
    encoded = urllib.parse.quote(name, safe="")
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{encoded}"
