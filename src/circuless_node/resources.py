"""Registering datasets and services (N5, F3, F4), and who may (N18).

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
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, model_validator
from pydantic import Field as PydanticField
from sqlmodel import Session, col, select

from .auth import require_subject
from .errors import NodeError, Reason
from .management import ManagementAction, enforce_management
from .models import CataloguePush, Resource, Tenant
from .settings import Settings
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
            tenant = _authorised_tenant(
                session, subject, tenant_slug, ManagementAction.RESOURCE_REGISTER
            )

            discoverability = body.discoverability or Discoverability.HIDDEN
            visibility = body.visibility or Visibility.ORG
            _check_classification(settings, body.classification)
            _check_discoverability(discoverability)
            _check_licence(body.licence, discoverability)

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
            tenant = _authorised_tenant(
                session, subject, tenant_slug, ManagementAction.RESOURCE_READ
            )
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
            tenant = _authorised_tenant(
                session, subject, tenant_slug, ManagementAction.RESOURCE_READ
            )
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
            tenant = _authorised_tenant(
                session, subject, tenant_slug, ManagementAction.RESOURCE_UPDATE
            )
            with tenant_scope(session, tenant.id):
                resource = _resource_or_404(session, resource_id)

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

                resource.updated_at = datetime.now(UTC)
                session.add(resource)
                mark_catalogue_dirty(session, tenant.id)
                session.commit()
                session.refresh(resource)
                return resource_out(resource)

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


def _authorised_tenant(
    session: Session, subject: Subject, tenant_slug: str, action: ManagementAction
) -> Tenant:
    """Resolve the tenant in the path, then decide whether this caller may act on it.

    Both halves, in this order, every time. Resolving alone gives a correctly-scoped
    query for an organisation the caller has nothing to do with — which is exactly the
    hole `tenancy.py` warns about: scoping is not authorisation.
    """
    tenant = tenant_by_slug(session, tenant_slug)
    enforce_management(subject, action, tenant_org(tenant))
    return tenant


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
    resource = session.get(Resource, resource_id)
    if resource is None or resource.status is ResourceStatus.WITHDRAWN:
        # A withdrawn resource is gone as far as any caller is concerned (D25), including
        # the one who withdrew it. Written now so that N20 does not have to find every
        # query that forgot.
        raise NodeError(404, Reason.NOT_FOUND, "no such resource")
    return resource
