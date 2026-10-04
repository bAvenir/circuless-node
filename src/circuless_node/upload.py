"""Putting a dataset's bytes on the node (N19, F4).

```
PUT /v1/t/{tenant}/resources/{id}/data          a file's bytes
PUT /v1/t/{tenant}/resources/{id}/data/{path}   one object of a bucket
```

## This is management, not consumption

The mirror image of `transfer.py` in the URL and nothing like it in authorisation.
Reading is `decide()` (N6) — visibility, agreements, acting organisation. Writing is
`decide_management` (N18): admins **or service principals** of the organisation that
owns the tenant, and nobody else. An agreement never grants write access; there is no
visibility under which an outsider may upload.

Keeping the two in separate modules is deliberate. They share a URL, which is exactly
why the authorisation difference wants to be impossible to miss while reading either one.

## The node decides where bytes go

`storage.resource_location` — `<tenant_id>/<resource_id>/<name>` — and not the
provider's `storage_path` taken literally. The reasoning is in that function; the short
version is that a provider-chosen location let one resource name another's file, which
turned "may register resources" into "may publish anything this organisation holds".

`storage_path` still names the file *within* the resource's own directory, so a provider
keeps control of what the download is called without controlling where it lands.

## Nothing is overwritten until it has fully arrived

Every upload streams to a temporary name and is renamed into place at the end. A
transfer that fails halfway, or runs past the size limit, leaves the previous bytes
exactly as they were — and a concurrent reader never sees a half-written file, which it
otherwise would, since N8 streams straight off disk.

## The size limit is checked twice

`Content-Length` first, so an oversized upload is refused before a byte is read. Then
again while streaming, because a chunked request carries no `Content-Length` at all and
a limit that trusts the header is a limit that can be skipped by not sending one.
"""

from __future__ import annotations

import uuid

import anyio.to_thread
from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from . import access_log
from .auth import require_subject
from .errors import NodeError, Reason
from .management import ManagementAction
from .models import Resource
from .resources import authorised_tenant, mark_catalogue_dirty
from .settings import Settings
from .storage import Storage, check_relative_path, resource_location, staging_location
from .subject import Subject
from .tenancy import tenant_scope
from .transfer import file_location
from .vocabularies import ResourceKind, ResourceStatus, Shape

#: Read from the request and written at a time. Larger than the download chunk because
#: each one costs a thread hop (see `_receive`), and 1 MiB makes that negligible even
#: for a gigabyte.
CHUNK_BYTES = 1024 * 1024


def upload_router() -> APIRouter:
    router = APIRouter()

    @router.put("/t/{tenant_slug}/resources/{resource_id}/data")
    async def upload_file(
        request: Request,
        tenant_slug: str,
        resource_id: uuid.UUID,
        subject: Subject = Depends(require_subject),
    ) -> dict:
        """Replace a `shape=file` resource's bytes."""
        resource, tenant_id, entry_id = _writable(request, subject, tenant_slug, resource_id)
        if resource.shape is not Shape.FILE:
            raise NodeError(
                400,
                Reason.UNSUPPORTED,
                "this resource is a bucket; upload its objects individually",
            )
        return await _store(request, tenant_id, resource, entry_id, file_location(resource))

    @router.put("/t/{tenant_slug}/resources/{resource_id}/data/{object_path:path}")
    async def upload_object(
        request: Request,
        tenant_slug: str,
        resource_id: uuid.UUID,
        object_path: str,
        subject: Subject = Depends(require_subject),
    ) -> dict:
        """Replace one object of a `shape=bucket` resource."""
        resource, tenant_id, entry_id = _writable(request, subject, tenant_slug, resource_id)
        if resource.shape is not Shape.BUCKET:
            raise NodeError(
                400,
                Reason.UNSUPPORTED,
                "this resource is a single file; upload to /data without a path",
            )
        # Before the join, for the reason `check_relative_path` documents: joining first
        # normalises away the very shapes the check is looking for.
        check_relative_path(object_path)
        return await _store(
            request,
            tenant_id,
            resource,
            entry_id,
            resource_location(resource.id, object_path),
        )

    return router


# --- authorisation --------------------------------------------------------------------


def _writable(
    request: Request, subject: Subject, tenant_slug: str, resource_id: uuid.UUID
) -> tuple[Resource, uuid.UUID, uuid.UUID]:
    """May this caller upload here, and is this resource something bytes can go into?

    `authorised_tenant` is N18 plus the N11 entry, exactly as the registration endpoints
    use it — so an upload is logged whether it is allowed or refused, without this
    module having to remember to do it.
    """
    with Session(request.app.state.engine) as session:
        authorised = authorised_tenant(
            request,
            session,
            subject,
            tenant_slug,
            ManagementAction.RESOURCE_UPLOAD,
            resource_id=resource_id,
        )
        with tenant_scope(session, authorised.tenant.id):
            resource = session.get(Resource, resource_id)
            if resource is None or resource.status is not ResourceStatus.ACTIVE:
                # Withdrawn is gone to everyone, including the organisation that owns it
                # (D25). Uploading into a withdrawn resource would quietly resurrect data
                # that a purge is already scheduled to remove.
                raise NodeError(404, Reason.NOT_FOUND, "no such resource")
            if resource.kind is not ResourceKind.DATASET:
                raise NodeError(
                    400, Reason.UNSUPPORTED, "a service has no stored data; register an endpoint"
                )
            session.expunge(resource)
    return resource, authorised.tenant.id, authorised.entry_id


# --- receiving ----------------------------------------------------------------------------


async def _store(
    request: Request,
    tenant_id: uuid.UUID,
    resource: Resource,
    entry_id: uuid.UUID,
    target: str,
) -> dict:
    settings: Settings = request.app.state.settings
    storage: Storage = request.app.state.storage
    limit = settings.max_upload_bytes

    _check_declared_length(request, limit)

    # Outside the resource's directory — see `STAGING_DIR`. Still under the tenant's
    # root, so the rename at the end is within one filesystem.
    staging = staging_location(f"{resource.id}-{uuid.uuid4().hex}")
    try:
        written = await _receive(request, storage, tenant_id, staging, limit)
    except BaseException:
        await anyio.to_thread.run_sync(storage.delete, tenant_id, staging)
        raise

    await anyio.to_thread.run_sync(storage.replace, tenant_id, staging, target)

    with Session(request.app.state.engine) as session, tenant_scope(session, tenant_id):
        # The distribution's size and modification date have changed, so the catalogue
        # record is stale. Marked, not pushed: pushing inside the request would make an
        # upload fail whenever the Cloud is unreachable (F16).
        mark_catalogue_dirty(session, tenant_id)
        session.commit()

    access_log.record_bytes(request.app.state.engine, entry_id, tenant_id, written)
    return {"bytes": written, "path": resource.storage_path or resource.slug}


def _check_declared_length(request: Request, limit: int) -> None:
    """Refuse before reading anything, when the client says how much is coming."""
    declared = request.headers.get("content-length")
    if declared is None:
        return
    try:
        length = int(declared)
    except ValueError:
        raise NodeError(400, Reason.INVALID_REQUEST, "malformed Content-Length") from None
    if length > limit:
        raise NodeError(
            413, Reason.PAYLOAD_TOO_LARGE, f"this node accepts at most {limit} bytes per upload"
        )


async def _receive(
    request: Request, storage: Storage, tenant_id: uuid.UUID, staging: str, limit: int
) -> int:
    """Stream the body to disk, counting, and stop the moment it goes over.

    Counted here rather than trusted from `Content-Length`: a chunked request has none,
    and a client that lies about it is the case the limit exists for.

    Writes go through `anyio.to_thread` because the handler is async and a synchronous
    write of a gigabyte would hold the event loop for the whole upload — stalling every
    other request on the node, including the downloads this is competing with for the
    same disk.
    """
    received = 0
    handle = await anyio.to_thread.run_sync(storage.open, tenant_id, staging, "wb")
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            received += len(chunk)
            if received > limit:
                raise NodeError(
                    413,
                    Reason.PAYLOAD_TOO_LARGE,
                    f"this node accepts at most {limit} bytes per upload",
                )
            await anyio.to_thread.run_sync(handle.write, chunk)
    finally:
        await anyio.to_thread.run_sync(handle.close)
    return received
