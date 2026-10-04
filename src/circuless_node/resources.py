"""Registering datasets and services (N5, F3, F4), who may (N18), and the record of it (N11).

The provider sends typed fields; the node composes the DCAT-AP record from them (`dcat`).
That is what makes the licence rule enforceable and the catalogue searchable — "has a
licence from the controlled list" is a column check here rather than a walk over
arbitrary provider JSON-LD.

## Three rules the endpoints exist to enforce

**Defaults are closed** (NFR4). A resource registered without sharing settings is
`discoverability=hidden` and `visibility=org`. Registering publishes nothing; advertising
it takes a second, deliberate call.

**A licence is required to publish** (NFR9). Null is allowed while `hidden`, because
registering something before the terms are settled is reasonable. Leaving `hidden`
without a licence from `vocabularies.LICENCES` is refused.

**A BVR-operated node refuses `sensitive`** (D22, H3), and defaults to being one — see
`settings.NodeOperator`. The permissive value is the one somebody has to type.

## Every management decision is logged, in one place

`_authorised_tenant` resolves the tenant, decides, and records — all three, every time.
It calls `decide_management` and raises the refusal itself rather than calling
`enforce_management`, because it needs the denial in its hands to log before it becomes
an exception. The logging lives there and not in each handler for the same reason the
tenant filter lives in one session listener: a guarantee that eight call sites have to
remember is not a guarantee.

## What is not here

Uploads (`PUT .../data`), deletion (N20, M3) and consumption (`decide()`, N6, M3). In
particular there is no `DELETE`: deletion is two-stage and belongs with the purge job, so
a placeholder that actually removed a row would be the wrong thing to have to take back.

## Changes mark the tenant, they do not push

Registering marks the tenant's catalogue dirty and returns; N7's loop pushes. Pushing
inside the request would make registration fail whenever the Cloud is unreachable,
turning a control-plane outage into a data-plane one — which is what F16 and D1 exist to
prevent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, model_validator
from pydantic import Field as PydanticField
from sqlmodel import Session, col, select

from . import access_log
from .auth import require_subject
from .errors import NodeError, Reason
from .management import ManagementAction, decide_management
from .models import CataloguePush, Resource, Tenant
from .settings import Settings
from .storage import check_relative_path
from .subject import Subject, organisations_from_groups
from .tenancy import tenant_by_slug, tenant_scope
from .vocabularies import (
    LICENCES,
    Classification,
    Discoverability,
    ResourceKind,
    ResourceStatus,
    Shape,
    Theme,
    Visibility,
)

SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"


# --- request shapes -----------------------------------------------------------------------


class ResourceIn(BaseModel):
    slug: str = PydanticField(pattern=SLUG_PATTERN, max_length=64)
    kind: ResourceKind
    title: str = PydanticField(min_length=1, max_length=255)
    description: str = PydanticField(default="", max_length=4000)
    theme: Theme
    classification: Classification

    licence: str | None = PydanticField(default=None, max_length=64)
    # Absent means the default, which is the closed end (NFR4). Spelled as None rather
    # than defaulted here so that "not supplied" and "explicitly hidden" stay
    # distinguishable, and the M2 exit criterion is about the former.
    discoverability: Discoverability | None = None
    visibility: Visibility | None = None

    shape: Shape | None = None
    storage_path: str | None = PydanticField(default=None, max_length=1024)
    endpoint_url: str | None = PydanticField(default=None, max_length=1024)
    openapi_ref: str | None = PydanticField(default=None, max_length=1024)
    invoke_policy: dict[str, Any] | None = None

    @model_validator(mode="after")
    def coherent_for_its_kind(self) -> ResourceIn:
        """A dataset and a service are different things, not one thing with optional bits.

        Checked here rather than in the handler so the refusal is a 422 naming the field,
        which is what a provider writing a registration script needs.
        """
        if self.kind is ResourceKind.DATASET:
            if self.shape is None:
                self.shape = Shape.FILE
            if self.shape is Shape.SERVICE:
                raise ValueError("a dataset cannot have shape=service")
            for field in ("endpoint_url", "openapi_ref", "invoke_policy"):
                if getattr(self, field) is not None:
                    raise ValueError(f"{field} belongs to a service, not a dataset")
        else:
            if self.shape is not None and self.shape is not Shape.SERVICE:
                raise ValueError("a service must have shape=service")
            self.shape = Shape.SERVICE
            if not self.endpoint_url:
                raise ValueError("a service needs an endpoint_url")
            if self.storage_path is not None:
                raise ValueError("storage_path belongs to a dataset, not a service")
        return self


class ResourcePatch(BaseModel):
    """Every field optional. `slug` and `kind` are absent — neither can change.

    A slug is how a provider's scripts refer to the resource, and the kind decides which
    DCAT-AP type the catalogue already published. Changing either is registering a
    different resource.
    """

    title: str | None = PydanticField(default=None, min_length=1, max_length=255)
    description: str | None = PydanticField(default=None, max_length=4000)
    theme: Theme | None = None
    licence: str | None = PydanticField(default=None, max_length=64)
    discoverability: Discoverability | None = None
    visibility: Visibility | None = None
    classification: Classification | None = None
    storage_path: str | None = PydanticField(default=None, max_length=1024)
    endpoint_url: str | None = PydanticField(default=None, max_length=1024)
    openapi_ref: str | None = PydanticField(default=None, max_length=1024)
    invoke_policy: dict[str, Any] | None = None


def resource_out(resource: Resource) -> dict:
    return {
        "id": str(resource.id),
        "slug": resource.slug,
        "kind": resource.kind.value,
        "shape": resource.shape.value,
        "status": resource.status.value,
        "title": resource.title,
        "description": resource.description,
        "theme": resource.theme.value,
        "licence": resource.licence,
        "discoverability": resource.discoverability.value,
        "visibility": resource.visibility.value,
        "classification": resource.classification.value,
        "storage_path": resource.storage_path,
        # The upstream address, shown to whoever may manage this resource and to nobody
        # else. It never reaches the catalogue: a consumer that knew it could go round
        # the node, past decide(), past the agreement check and past the log.
        "endpoint_url": resource.endpoint_url,
        "openapi_ref": resource.openapi_ref,
        "invoke_policy": resource.invoke_policy,
        "created_at": resource.created_at.isoformat(),
        "updated_at": resource.updated_at.isoformat(),
        # Null until a DELETE. Shown so the owner can see a purge is pending, and when
        # (N20, D25) — the whole point of making withdrawn resources visible to them.
        "withdrawn_at": resource.withdrawn_at.isoformat() if resource.withdrawn_at else None,
        "purge_after": resource.purge_after.isoformat() if resource.purge_after else None,
    }


# --- the router -----------------------------------------------------------------------------


def resource_router() -> APIRouter:
    router = APIRouter()

    @router.post("/t/{tenant_slug}/resources", status_code=201)
    def register(
        request: Request,
        tenant_slug: str,
        body: ResourceIn,
        subject: Subject = Depends(require_subject),
    ) -> dict:
        settings: Settings = request.app.state.settings
        with Session(request.app.state.engine) as session:
            tenant = authorised_tenant(
                request, session, subject, tenant_slug, ManagementAction.RESOURCE_REGISTER
            ).tenant

            discoverability = body.discoverability or Discoverability.HIDDEN
            visibility = body.visibility or Visibility.ORG
            _check_classification(settings, body.classification)
            _check_discoverability(discoverability)
            _check_licence(body.licence, discoverability)
            _check_storage_path(body.storage_path)

            with tenant_scope(session, tenant.id):
                if session.exec(select(Resource).where(col(Resource.slug) == body.slug)).first():
                    raise NodeError(
                        409, Reason.CONFLICT, "this tenant already has a resource with that slug"
                    )

                resource = Resource(
                    tenant_id=tenant.id,
                    slug=body.slug,
                    kind=body.kind,
                    shape=body.shape or Shape.FILE,
                    title=body.title,
                    description=body.description,
                    theme=body.theme,
                    licence=body.licence,
                    discoverability=discoverability,
                    visibility=visibility,
                    classification=body.classification,
                    storage_path=body.storage_path,
                    endpoint_url=body.endpoint_url,
                    openapi_ref=body.openapi_ref,
                    invoke_policy=body.invoke_policy,
                )
                session.add(resource)
                mark_catalogue_dirty(session, tenant.id)
                session.commit()
                session.refresh(resource)
                return resource_out(resource)

    @router.get("/t/{tenant_slug}/resources")
    def list_resources(
        request: Request,
        tenant_slug: str,
        subject: Subject = Depends(require_subject),
    ) -> list[dict]:
        """Every resource of this tenant, for a caller who may manage them.

        Not filtered by `visibility` — that is consumption, and `decide()` (N6) owns it.
        A management caller is an admin or a service of the owning organisation, and
        `visibility=private` is about consumers rather than about them.
        """
        with Session(request.app.state.engine) as session:
            tenant = authorised_tenant(
                request, session, subject, tenant_slug, ManagementAction.RESOURCE_READ
            ).tenant
            with tenant_scope(session, tenant.id):
                found = session.exec(select(Resource).order_by(col(Resource.slug))).all()
                return [resource_out(resource) for resource in found]

    @router.get("/t/{tenant_slug}/resources/{resource_id}")
    def read_resource(
        request: Request,
        tenant_slug: str,
        resource_id: uuid.UUID,
        subject: Subject = Depends(require_subject),
    ) -> dict:
        with Session(request.app.state.engine) as session:
            tenant = authorised_tenant(
                request,
                session,
                subject,
                tenant_slug,
                ManagementAction.RESOURCE_READ,
                resource_id=resource_id,
            ).tenant
            with tenant_scope(session, tenant.id):
                return resource_out(_resource_or_404(session, resource_id))

    @router.patch("/t/{tenant_slug}/resources/{resource_id}")
    def update_resource(
        request: Request,
        tenant_slug: str,
        resource_id: uuid.UUID,
        body: ResourcePatch,
        subject: Subject = Depends(require_subject),
    ) -> dict:
        settings: Settings = request.app.state.settings
        with Session(request.app.state.engine) as session:
            tenant = authorised_tenant(
                request,
                session,
                subject,
                tenant_slug,
                ManagementAction.RESOURCE_UPDATE,
                resource_id=resource_id,
            ).tenant
            with tenant_scope(session, tenant.id):
                resource = _active_or_conflict(session, resource_id)

                changes = body.model_dump(exclude_unset=True)
                for field, value in changes.items():
                    setattr(resource, field, value)

                # Re-checked against the resulting state, not against what was sent: a
                # patch that only raises discoverability has to satisfy the licence rule
                # using the licence already there, and one that only clears the licence
                # has to satisfy it against the discoverability already there.
                _check_classification(settings, resource.classification)
                _check_discoverability(resource.discoverability)
                _check_licence(resource.licence, resource.discoverability)
                _check_storage_path(resource.storage_path)

                resource.updated_at = datetime.now(UTC)
                session.add(resource)
                mark_catalogue_dirty(session, tenant.id)
                session.commit()
                session.refresh(resource)
                return resource_out(resource)

    @router.delete("/t/{tenant_slug}/resources/{resource_id}")
    def withdraw_resource(
        request: Request,
        tenant_slug: str,
        resource_id: uuid.UUID,
        subject: Subject = Depends(require_subject),
    ) -> dict:
        """Stage one of two (N20, D25). Marks the resource withdrawn; removes nothing.

        From this moment `decide()` denies every read and invoke with `not_found`, and
        the next sync drops the record from the catalogue push — the Cloud tombstones
        what a push no longer contains, so withdrawal propagates by absence rather than
        by a second call that could fail on its own.

        The bytes stay until `purge_after`. That gap is the point: deletion that takes
        effect instantly and irreversibly is deletion nobody dares use, and a provider
        who removes the wrong dataset has until then to say so.

        **Not idempotent.** A second `DELETE` is a 409, because by then the resource is
        visible to its owner as withdrawn-and-scheduled, and answering "done" would
        hide the fact that the purge date was set by the *first* call and has not moved.
        """
        settings: Settings = request.app.state.settings
        with Session(request.app.state.engine) as session:
            tenant = authorised_tenant(
                request,
                session,
                subject,
                tenant_slug,
                ManagementAction.RESOURCE_DELETE,
                resource_id=resource_id,
            ).tenant
            with tenant_scope(session, tenant.id):
                resource = _active_or_conflict(session, resource_id)

                now = datetime.now(UTC)
                resource.status = ResourceStatus.WITHDRAWN
                resource.withdrawn_at = now
                resource.purge_after = now + timedelta(days=settings.purge_after_days)
                resource.updated_at = now
                session.add(resource)
                mark_catalogue_dirty(session, tenant.id)
                session.commit()
                session.refresh(resource)
                return resource_out(resource)

    @router.get("/t/{tenant_slug}/access-log")
    def read_access_log(
        request: Request,
        tenant_slug: str,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        resource_id: uuid.UUID | None = None,
        decision: str | None = Query(default=None, pattern="^(allow|deny)$"),
        subject: Subject = Depends(require_subject),
    ) -> dict:
        """Every decision this node made about this tenant's resources (N11).

        Admins of the owning organisation only, which `decide_management` enforces —
        and this read is itself a management decision, so it appears in the log it
        returns. That is intended: "who has been reading the access log" is exactly the
        sort of question an access log should be able to answer about itself.

        Newest first, and paged rather than streamed: this is an investigation tool, and
        an operator who needs the whole thing has the database.
        """
        with Session(request.app.state.engine) as session:
            tenant = authorised_tenant(
                request, session, subject, tenant_slug, ManagementAction.ACCESS_LOG_READ
            ).tenant
            with tenant_scope(session, tenant.id):
                found = access_log.entries(
                    session,
                    limit=limit,
                    offset=offset,
                    resource_id=resource_id,
                    decision=decision,
                )
                return {
                    "entries": [access_log.entry_out(entry) for entry in found],
                    "limit": limit,
                    "offset": offset,
                }

    return router


# --- rules ------------------------------------------------------------------------------------


def _check_classification(settings: Settings, classification: Classification) -> None:
    """D22, H3. A BVR-operated node refuses sensitive data.

    Not because this node is less secure — it is the better-run of the two today — but
    because BVR holding a partner's sensitive data on their behalf is the arrangement the
    project undertook not to make.
    """
    if classification is Classification.SENSITIVE and settings.refuses_sensitive:
        raise NodeError(
            422,
            Reason.CLASSIFICATION_NOT_PERMITTED,
            "this node does not hold sensitive data; register it on your own node",
        )


def _check_discoverability(discoverability: Discoverability) -> None:
    """D21, design §5.2. `public` is reserved for later and is not available now.

    `public` means discoverable *anonymously*, and the beta has no anonymous access at
    all — so accepting it would record an intention the platform cannot act on, and the
    owner would believe they had published more widely than they had.

    Refused here as well as at the Cloud's catalogue (C5), deliberately. Here, because a
    provider should be told at the moment they ask rather than discovering 30 seconds
    later that a push they cannot see was refused. There, because the Cloud should not
    depend on every node running a version that knows this rule.
    """
    if discoverability is Discoverability.PUBLIC:
        raise NodeError(
            422,
            Reason.UNSUPPORTED,
            "discoverability 'public' is reserved for later and is not available in "
            "the beta; use 'catalogue', which every authenticated participant can see",
        )


def _check_storage_path(storage_path: str | None) -> None:
    """The file's name inside its own directory — checked when it is typed, not when
    it is used.

    Since N19 the node owns the layout: bytes live at `<tenant>/<resource_id>/<name>`
    and `storage_path` is only the name within that directory. Confinement still holds
    whatever is stored here, because `Storage.resolve` checks the joined path — but
    refusing a `..` at registration tells the provider at the moment they got it wrong,
    rather than at the first upload.
    """
    if storage_path is None:
        return
    check_relative_path(storage_path)


def _check_licence(licence: str | None, discoverability: Discoverability) -> None:
    """NFR9. A licence from the controlled list is required before publishing."""
    if licence is not None and licence not in LICENCES:
        raise NodeError(
            422,
            Reason.INVALID_REQUEST,
            f"unknown licence; use one of: {', '.join(sorted(LICENCES))}",
        )
    if discoverability is not Discoverability.HIDDEN and licence is None:
        raise NodeError(
            422,
            Reason.LICENCE_REQUIRED,
            "a resource needs a licence before it can be discoverable",
        )


def mark_catalogue_dirty(session: Session, tenant_id: uuid.UUID) -> None:
    """Record that this tenant's catalogue has changed, for N7 to push.

    One row per tenant, upserted: what N7 sends is the tenant's whole catalogue, so ten
    edits before the next push are one push, and a push lost to a restart is still
    pending afterwards rather than lost.
    """
    existing = session.get(CataloguePush, tenant_id)
    if existing is None:
        session.add(CataloguePush(tenant_id=tenant_id))
    else:
        existing.marked_at = datetime.now(UTC)
        session.add(existing)


# --- helpers ------------------------------------------------------------------------------------


class Authorised(NamedTuple):
    """What `authorised_tenant` hands back: the tenant, and the log entry it just wrote.

    The entry id is here so that an upload (N19) can fill in `bytes` once the body has
    arrived. Everything else ignores it.
    """

    tenant: Tenant
    entry_id: uuid.UUID


def authorised_tenant(
    request: Request,
    session: Session,
    subject: Subject,
    tenant_slug: str,
    action: ManagementAction,
    resource_id: uuid.UUID | None = None,
) -> Authorised:
    """Resolve the tenant in the path, decide whether this caller may act on it, log it.

    All three, in this order, every time. Resolving alone gives a correctly-scoped query
    for an organisation the caller has nothing to do with — which is exactly the hole
    `tenancy.py` warns about: scoping is not authorisation.

    The logging lives here rather than in each handler, for the same reason the tenant
    filter lives in one session listener: a guarantee that each of eight call sites has
    to remember is not a guarantee. This is why the function calls `decide_management`
    and raises itself, instead of calling `enforce_management` — it needs the denial in
    its hands to record before it becomes an exception.

    **An unknown tenant is not logged.** There is no tenant to own the entry, and
    `AccessLog` is tenant-owned by design; more to the point, "someone asked about a
    tenant we do not host" is a fact about this node, not a decision about anyone's
    resources. It is a 404 from `tenant_by_slug` and goes no further.
    """
    tenant = tenant_by_slug(session, tenant_slug)
    owner = tenant_org(tenant)
    decision = decide_management(subject, action, owner)

    entry_id = access_log.record(
        request.app.state.engine,
        tenant_id=tenant.id,
        request_id=access_log.request_id_of(request),
        action=action.value,
        subject=subject,
        allowed=decision.allowed,
        reason=decision.reason,
        resource_id=resource_id,
        # On a management allow, the organisation acted for is the tenant's owner — that
        # is what `decide_management` checked membership of, and there is no choice to
        # resolve (R11). Null on a denial: nothing of the caller's was accepted.
        acting_org=owner if decision.allowed else None,
    )

    if not decision.allowed:
        raise NodeError(403, decision.reason or Reason.NOT_PERMITTED, decision.detail)
    return Authorised(tenant, entry_id)


def tenant_org(tenant: Tenant) -> str:
    """The slug of the organisation that owns this tenant.

    Parsed from `group_path` with the same function that reads a token's groups, so the
    string compared against `subject.admin_of` is produced by one reading of what a group
    path means rather than two.
    """
    orgs, _admins = organisations_from_groups([tenant.group_path])
    if not orgs:
        # A tenant row whose group_path is not a depth-one /orgs/<x> cannot be owned by
        # anyone a token could name, so nobody can manage it. Loud, because it is a
        # provisioning bug and not something a caller can cause.
        raise NodeError(500, Reason.INTERNAL_ERROR, "tenant has no resolvable owning organisation")
    return next(iter(orgs))


def _resource_or_404(session: Session, resource_id: uuid.UUID) -> Resource:
    """Any resource of this tenant, withdrawn ones included.

    **Management sees withdrawn resources; consumption never does.** D25 says a
    withdrawn resource is gone to everyone, and that is enforced where it matters — in
    `decide()`, which denies it `not_found`, so no consumer and no byte ever reaches
    one. This is the organisation looking at its own registry, which is a different
    question: without it, a provider who deleted the wrong resource has no way to find
    out, and no way to see that a purge is pending while there is still time to care.

    Until N20 this function refused them outright, while `GET /resources` listed them —
    the two disagreed, and settling it in favour of the owner is what that resolves.
    """
    resource = session.get(Resource, resource_id)
    if resource is None:
        raise NodeError(404, Reason.NOT_FOUND, "no such resource")
    return resource


def _active_or_conflict(session: Session, resource_id: uuid.UUID) -> Resource:
    """…and a withdrawn one cannot be changed.

    `409`, not `404`: the caller can see it, so pretending it is absent would be a
    worse answer than saying it is on its way out. Editing or re-uploading would
    quietly resurrect something a purge is already scheduled to remove.
    """
    resource = _resource_or_404(session, resource_id)
    if resource.status is ResourceStatus.WITHDRAWN:
        raise NodeError(
            409,
            Reason.CONFLICT,
            "this resource is withdrawn and awaiting purge; it cannot be changed",
        )
    return resource
