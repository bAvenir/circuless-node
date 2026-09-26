"""Persistent model.

Two families, and the difference is load-bearing (R10):

  * **Tenant-owned** tables carry `tenant_id` and are filtered once, centrally, by the
    tenancy layer (N4). A handler never writes its own tenant filter.
  * **Node-global** tables — AgreementCache, OrgMap, NodeIdentity — have no tenant and are
    explicitly excluded from that filter. Applying it to them would break sync, because the
    agreements a node enforces belong to no single tenant.

Only Tenant exists so far; the rest arrive with the components that own them. Alembic runs
from this first model, so every later table comes as a migration rather than a schema edit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


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
