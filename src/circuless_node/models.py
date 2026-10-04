"""Persistent model.

Two families, and the difference is load-bearing (R10):

  * **Tenant-owned** tables carry `tenant_id` and are filtered once, centrally, by the
    tenancy layer (N4). A handler never writes its own tenant filter.
  * **Node-global** tables — AgreementCache, OrgMap, NodeIdentity — have no tenant and are
    explicitly excluded from that filter. Applying it to them would break sync, because the
    agreements a node enforces belong to no single tenant.

Alembic runs from the first model, so every later table comes as a migration rather than
a schema edit someone applied by hand.

Still to arrive: `ServiceCredential` (N10).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, UniqueConstraint
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel

from .vocabularies import (
    Classification,
    Discoverability,
    ResourceKind,
    ResourceStatus,
    Shape,
    Theme,
    Visibility,
)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


class TenantOwned(SQLModel):
    """Marker for tables the tenancy filter applies to (N4).

    Subclassing this is what opts a table in, so a new table is filtered by virtue of its
    shape rather than by someone remembering to add it to a list.
    """

    tenant_id: uuid.UUID = Field(foreign_key="tenant.id", index=True)


class NodeIdentity(SQLModel, table=True):
    """What this node knows about its own credentials (N17).

    Node-global: it is about the node, not about any organisation, so the tenancy filter
    must never touch it (R10).

    The private key is a file, not a row — it never goes near the database. What is
    recorded here is which certificate is in use and since when, so an operator can ask
    "is the certificate Keycloak holds the one this node is signing with?" and check a
    fingerprint rather than handle a key. Rotation will extend this with the retired
    fingerprint and a retire-after time.
    """

    id: uuid.UUID = Field(default_factory=_uuid, primary_key=True)
    node_id: str = Field(index=True, unique=True)
    client_id: str
    certificate_fingerprint: str
    created_at: datetime = Field(default_factory=_now)


class Tenant(SQLModel, table=True):
    """An organisation hosted on this node.

    Node-global: tenants are the thing the filter keys on, so they cannot themselves be
    filtered by it. `org_id` and `group_path` come from the Cloud's org registry, which
    exists because no stock Keycloak mapper emits group attributes (§3.2).
    """

    id: uuid.UUID = Field(default_factory=_uuid, primary_key=True)
    org_id: uuid.UUID = Field(index=True, unique=True)
    # The Keycloak group, e.g. /orgs/alpha. Depth one under /orgs, always.
    group_path: str = Field(index=True, unique=True)
    slug: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=_now)


class Resource(TenantOwned, table=True):
    """A dataset or a service offered by one tenant (N5, F3, F4).

    **Tenant-owned by shape.** Subclassing `TenantOwned` is what puts it under N4's
    filter, so every query about resources is scoped without any handler writing
    `WHERE tenant_id = ...`, and an unscoped one raises rather than returning everything.

    ## Two settings that are easy to confuse

    `discoverability` governs who may learn the resource **exists** — what reaches the
    Cloud catalogue and what a search returns. `visibility` governs who may **read or
    invoke** it, and is decided by `decide()` (N6). They are independent: a resource can
    be listed publicly and readable only under an agreement, which is the normal case for
    something worth discovering.

    Both default to the closed end — `hidden` and `org` (NFR4). Registering a resource
    publishes nothing; that takes a second, deliberate call.

    ## The licence rule

    `licence` may be null while a resource is `hidden`, because a provider registering
    something before they have settled the terms is a reasonable thing to do. It must be
    present, and from the controlled list, before `discoverability` leaves `hidden`
    (NFR9). Enforced in `resources.py` rather than by a NOT NULL, because the constraint
    is conditional on another column.
    """

    __tablename__ = "resource"
    __table_args__ = (
        # A slug is how a provider refers to their own resource in a script that does not
        # want to hold a UUID. Unique per tenant, never globally: two organisations naming
        # something `batch-7` is not a conflict, and making it one would leak the fact
        # that the other name exists.
        UniqueConstraint("tenant_id", "slug", name="uq_resource_tenant_slug"),
    )

    id: uuid.UUID = Field(default_factory=_uuid, primary_key=True)
    slug: str = Field(index=True, max_length=64)

    kind: ResourceKind = Field(max_length=16)
    shape: Shape = Field(max_length=16)
    status: ResourceStatus = Field(default=ResourceStatus.ACTIVE, max_length=16, index=True)

    # --- what the catalogue shows -------------------------------------------------------
    title: str = Field(max_length=255)
    description: str = Field(default="", max_length=4000)
    theme: Theme = Field(max_length=32)
    #: An SPDX identifier or EU Vocabularies key from `vocabularies.LICENCES`, never a
    #: free URI: an unresolvable licence is worse than none, because it looks like one.
    licence: str | None = Field(default=None, max_length=64)

    # --- sharing ------------------------------------------------------------------------
    discoverability: Discoverability = Field(
        default=Discoverability.HIDDEN, max_length=16, index=True
    )
    visibility: Visibility = Field(default=Visibility.ORG, max_length=16, index=True)
    classification: Classification = Field(max_length=16)

    # --- where the content is -----------------------------------------------------------
    #: Datasets. Relative to the tenant's directory, resolved by `storage.Storage`, which
    #: refuses anything escaping it.
    storage_path: str | None = Field(default=None, max_length=1024)
    #: Services. Where the node proxies `/invoke` to — never returned to a consumer, who
    #: reaches the service only through this node.
    endpoint_url: str | None = Field(default=None, max_length=1024)
    #: Services. A URL, or a document stored like any other content.
    openapi_ref: str | None = Field(default=None, max_length=1024)
    #: Services. Timeouts, size limits, streaming — declared by the provider at
    #: registration and read by N9 when it proxies. JSON because its shape is the
    #: provider's, not ours.
    invoke_policy: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    # --- two-stage deletion (D25, N20 in M3) ---------------------------------------------
    withdrawn_at: datetime | None = Field(default=None)
    purge_after: datetime | None = Field(default=None)

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class CataloguePush(SQLModel, table=True):
    """Which tenants have catalogue changes the Cloud has not been told about yet (N5→N7).

    **Node-global**, and deliberately so: it is about this node's relationship with the
    Cloud, not about any tenant's data, so N4's filter must not touch it (R10). The
    `tenant_id` here is a payload, not an ownership marker.

    Registering a resource marks the tenant dirty and returns. N7's loop pushes the
    catalogue for dirty tenants and clears the flag. Pushing inside the request would
    make registration fail whenever the Cloud is unreachable, turning a control-plane
    outage into a data-plane one — which is the thing the cached-enforcement design
    (F16, D1) exists to avoid.

    One row per tenant, not per change: what N7 sends is the tenant's whole catalogue, so
    ten edits before the next push are one push, and a push lost to a restart is still
    pending afterwards rather than lost.
    """

    __tablename__ = "catalogue_push"

    tenant_id: uuid.UUID = Field(foreign_key="tenant.id", primary_key=True)
    #: When the catalogue last changed. N7 clears the row once it has pushed; a row that
    #: reappears during a push is a change that arrived mid-flight and must not be lost.
    marked_at: datetime = Field(default_factory=_now)


class OrgMap(SQLModel, table=True):
    """The Cloud's organisation registry, as this node last saw it (N7, F1).

    **Node-global** (R10): it describes the platform, not one tenant's data, and the sync
    that maintains it belongs to no tenant. Filtering it would break the pull.

    Exists because no stock Keycloak mapper emits group attributes, so the mapping from a
    group path to a stable id has to be carried somewhere (§3.2).

    `slug` is what the node compares against — a token yields `/orgs/alpha`, which parses
    to `alpha`, and `Subject.org_ids` holds slugs. `org_id` is carried for the day both
    sides move to UUIDs, which `docs/identity-contract.md` in the Cloud repository says
    has to happen on both at once.
    """

    __tablename__ = "org_map"

    slug: str = Field(primary_key=True, max_length=64)
    org_id: uuid.UUID = Field(index=True)
    group_path: str = Field(index=True, max_length=255)
    display_name: str = Field(default="", max_length=255)
    synced_at: datetime = Field(default_factory=_now)


class AgreementCache(SQLModel, table=True):
    """Agreements this node enforces, as the Cloud last sent them (N7, F7, F14).

    **Node-global** (R10), and this is the case that makes the distinction load-bearing:
    an agreement is between two organisations, so it belongs to neither tenant's rows.
    Filtering it by tenant would return nothing and quietly deny every cross-org read.

    **Provider side only** (R4). The Cloud sends agreements where an organisation *hosted
    on this node* is the provider — never another organisation's consumer-side
    arrangements. A node therefore cannot learn what its tenants are buying elsewhere,
    which matters when the node is operated by a competitor of the other party.

    **A cache, not a record.** The Cloud owns agreements (§4.5); this is a copy kept so
    that `decide()` needs no network call on the request path, and so enforcement
    continues through a Cloud outage (F16). A pull replaces the set wholesale, because a
    revocation is an absence and a merge would never notice one.
    """

    __tablename__ = "agreement_cache"

    id: uuid.UUID = Field(primary_key=True)
    provider_org: str = Field(index=True, max_length=64)
    consumer_org: str = Field(index=True, max_length=64)
    #: Null means every resource of the provider, which is how a blanket agreement is
    #: expressed. Not a sentinel UUID: null is the thing SQL can answer questions about.
    resource_id: uuid.UUID | None = Field(default=None, index=True)
    #: A subset of {read, invoke}, stored as a sorted comma-separated list. A JSON array
    #: would be more natural and less queryable; there are two possible values.
    actions: str = Field(max_length=32)
    valid_from: datetime
    valid_until: datetime | None = Field(default=None)
    status: str = Field(max_length=16, index=True)
    #: The CIRCULess ODRL 2.2 subset, opaque here. `decide()` reads it in N6; the node
    #: never edits it, so its shape is the Cloud's business.
    policy: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    synced_at: datetime = Field(default_factory=_now)

    def permits(self, action: str) -> bool:
        return action in self.actions.split(",")


class AccessLog(TenantOwned, table=True):
    """Every decision this node made, allow and deny (N11, invariant 12, F17).

    **Tenant-owned**, so N4's filter covers it by shape: an organisation's admins read
    their own log and cannot see another's, without any handler writing a filter.

    ## Append-only, with one exception that is enforced rather than promised

    A recorded decision can never be altered or removed. `bytes` is the single exception
    and is write-once: it is only known after a transfer has streamed, and the entry has
    to exist before that or a connection dropped mid-stream would leave no record that
    access was ever granted.

    The migration's triggers enforce exactly that — `bytes` null to a value once,
    nothing else ever, no deletes. Measured on SQLite rather than assumed.

    ## Pseudonymous, and it stays that way

    `subject_sub` is Keycloak's UUID. **Never a name or an email** (D31): node tokens do
    not carry them, and a log that grew them would become the thing it protects. Names
    are resolved at display time, by whoever is entitled to see them, and never stored
    here.

    ## It outlives what it describes

    N20's purge removes a withdrawn resource's data and metadata after the retention
    period; these entries stay. `resource_id` therefore often points at something that
    no longer exists, which is correct — the question an audit answers is what happened,
    not what is still there.
    """

    __tablename__ = "access_log"

    id: uuid.UUID = Field(default_factory=_uuid, primary_key=True)
    ts: datetime = Field(default_factory=_now, index=True)

    #: Minted by this node per request and echoed in the response, never taken from an
    #: inbound header: an id the caller controls can be repeated or collided with, which
    #: is worth something to whoever is being investigated.
    request_id: str = Field(index=True, max_length=64)

    #: Null for management actions that name no resource — listing, for instance.
    resource_id: uuid.UUID | None = Field(default=None, index=True)

    #: `read` or `invoke` for consumption, or a `ManagementAction` value. One column,
    #: because "what did they try to do" is one question.
    action: str = Field(index=True, max_length=32)

    subject_sub: str = Field(index=True, max_length=255)
    principal_type: str = Field(max_length=16)
    #: `azp` — the client the token was issued to, and the only trace of a service
    #: acting on someone's behalf, since token exchange carries no `act` claim (Q2).
    actor_azp: str | None = Field(default=None, max_length=255)
    #: On whose behalf (R11). Null where nothing of the caller's is what permitted it —
    #: `public` visibility — and on a denial that never got that far.
    acting_org: str | None = Field(default=None, index=True, max_length=64)

    #: `allow` or `deny`. A string rather than a boolean so a reader of the raw table
    #: cannot mistake which way round it is.
    decision: str = Field(index=True, max_length=8)
    #: The reason code on a denial, from the one enum. Null on an allow.
    reason: str | None = Field(default=None, max_length=64)

    #: Write-once, and only for transfers. Null means "not a transfer, or it never
    #: completed" — the two are distinguished by `decision`.
    bytes: int | None = Field(default=None)
