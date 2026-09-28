"""One process, several organisations (N4).

Every tenant-owned query is filtered once, centrally, by a session-level
`with_loader_criteria` — so no handler ever writes `WHERE tenant_id = ...` itself, and a
handler that forgets cannot leak anything. Isolation is **code-enforced, not OS-enforced**;
that is a declared limitation (§4.3) and belongs in D2.1 and the T2.7 material.

**What is filtered, and what must not be.** A table opts in by subclassing `TenantOwned`,
so it is the shape of the model that decides, not a list somewhere that someone has to
remember to update. The node-global tables — `AgreementCache`, `OrgMap`, `NodeIdentity` —
deliberately do not: filtering them would break sync, because the agreements a node
enforces belong to no single tenant (R10).

**Unscoped queries fail rather than return everything.** If the filter simply did nothing
when no tenant was bound, forgetting to bind would silently disable isolation — the worst
possible default, because it looks like it works. Reaching across tenants is legitimate in
a few places (the purge job, N20), so it is available, but only by saying so out loud with
`all_tenants()`.

## What this does not do

Scoping is not authorisation. This layer guarantees that a query about tenant A returns
only tenant A's rows. It says nothing about whether *this caller* may act on tenant A at
all — that is `decide()` for consumption (N6) and management authorisation for the rest
(N18). Conflating the two is how a hole appears: a caller from another organisation
reaching `/v1/t/alpha/...` gets correctly-scoped Alpha data unless something else stops
them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import with_loader_criteria
from sqlmodel import Session, select

from .errors import NodeError, Reason
from .models import Tenant, TenantOwned

#: Key under which the active scope lives in `Session.info`.
SCOPE_KEY = "circuless_tenant_scope"


class AllTenants:
    """Sentinel for a deliberately unscoped session. See `all_tenants()`."""

    def __repr__(self) -> str:
        return "ALL_TENANTS"


ALL_TENANTS = AllTenants()


class TenancyNotScopedError(RuntimeError):
    """A tenant-owned table was queried on a session with no tenant bound.

    Always a bug in the node, never something a caller can provoke, so it is a
    RuntimeError rather than a NodeError — it should reach the logs as a 500 and be fixed,
    not be handed to the caller as a reason code.
    """


# Registered on the Session class at import, not per engine or per session. There is
# deliberately no `install_tenancy()` to call: an isolation guarantee that depends on
# someone remembering to switch it on is not a guarantee.
@event.listens_for(SASession, "do_orm_execute")
def _scope_tenant_owned_reads(execute_state: Any) -> None:
    # Column and relationship loads are continuations of a query whose criteria were
    # already applied; re-filtering them would be redundant and can break eager loads.
    if not execute_state.is_select:
        return
    if execute_state.is_column_load or execute_state.is_relationship_load:
        return

    tenant_owned = _tenant_owned_entities(execute_state)
    if not tenant_owned:
        return

    scope = execute_state.session.info.get(SCOPE_KEY)
    if scope is None:
        raise TenancyNotScopedError(
            "a tenant-owned table was queried without a tenant scope. Use "
            "tenant_scope(session, tenant_id), or all_tenants(session) where crossing "
            "tenants is intended."
        )
    if scope is ALL_TENANTS:
        return

    # One criterion per concrete mapped class, rather than one against `TenantOwned`.
    # `TenantOwned` is a non-table SQLModel, so its `tenant_id` is a pydantic field and
    # not a SQLAlchemy column — passing the base makes SQLAlchemy raise AttributeError
    # when it tries to build the comparison. The concrete classes are properly mapped.
    #
    # A query joining two tenant-owned tables gets a criterion for each, which is what we
    # want: both sides have to belong to this tenant.
    for entity in tenant_owned:
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                entity,
                entity.tenant_id == scope,
                include_aliases=True,
            )
        )


def _tenant_owned_entities(execute_state: Any) -> list[type[TenantOwned]]:
    return [
        mapper.class_
        for mapper in execute_state.all_mappers
        if isinstance(mapper.class_, type) and issubclass(mapper.class_, TenantOwned)
    ]


@event.listens_for(SASession, "before_flush")
def _scope_tenant_owned_writes(session: SASession, _context: Any, _instances: Any) -> None:
    """Writes are scoped too.

    `with_loader_criteria` only touches SELECT, so without this a handler could insert a
    row carrying someone else's `tenant_id` — and then never see it again, which is a
    confusing way to discover a bug. Cheap to check, and it turns a data-corruption class
    of mistake into an immediate failure.
    """
    scope = session.info.get(SCOPE_KEY)
    if scope is None or scope is ALL_TENANTS:
        return

    for instance in (*session.new, *session.dirty):
        if isinstance(instance, TenantOwned) and instance.tenant_id != scope:
            raise TenancyNotScopedError(
                f"{type(instance).__name__} carries tenant_id={instance.tenant_id!r} "
                f"but the session is scoped to {scope!r}"
            )


@contextmanager
def tenant_scope(session: Session, tenant_id: uuid.UUID) -> Iterator[Session]:
    """Bind a session to one tenant for the duration of the block."""
    previous = session.info.get(SCOPE_KEY)
    session.info[SCOPE_KEY] = tenant_id
    try:
        yield session
    finally:
        _restore(session, previous)


@contextmanager
def all_tenants(session: Session) -> Iterator[Session]:
    """Deliberately cross tenants.

    For the few jobs that are about the node rather than about one organisation — the
    purge job (N20) above all. Never reachable from a request handler: a handler always
    knows which tenant it is serving, and if it does not, that is the bug.
    """
    previous = session.info.get(SCOPE_KEY)
    session.info[SCOPE_KEY] = ALL_TENANTS
    try:
        yield session
    finally:
        _restore(session, previous)


def _restore(session: Session, previous: Any) -> None:
    if previous is None:
        session.info.pop(SCOPE_KEY, None)
    else:
        session.info[SCOPE_KEY] = previous


def tenant_by_slug(session: Session, slug: str) -> Tenant:
    """Look up the tenant a `/v1/t/{slug}/...` route names.

    `Tenant` is node-global and so is not itself filtered — it is what the filter keys on,
    and a tenant that could not be read without already knowing its id would be useless.

    A slug this node does not host is `not_found` rather than a distinct code: whether an
    organisation exists elsewhere in CIRCULess is not this node's to disclose.
    """
    tenant = session.exec(select(Tenant).where(Tenant.slug == slug)).first()
    if tenant is None:
        raise NodeError(404, Reason.NOT_FOUND, "no such tenant on this node")
    return tenant
