"""Which tenants can this caller reach on this node?

```
GET /v1/tenants
```

A node hosts several organisations, and every other route names one in its path. Until
now nothing told a caller which names were valid, and they cannot be derived from the
token: `Tenant.slug` is its own column, nothing constrains it to equal the slug in the
owning group's `group_path`, and a token carries organisation slugs only. `alpha` the
organisation can be `alpha-prod` the tenant.

So an interface either asked someone to type a name they might not know, or this exists.

## What it discloses, which is nothing new

Only tenants whose owning organisation the caller belongs to. A member of Alpha learns
that this node hosts Alpha — which they learn from the first request they make anyway —
and learns nothing at all about Beta.

## `can_manage` is here so an interface can say something useful

Membership and management are different rights (N18): being in an organisation lets you
consume under `visibility=org`, while registering, uploading and reading the log need an
admin or a service principal. A listing that showed only manageable tenants would make
"you are a member here but may not publish" look like "this node does not host you",
which is the more alarming of the two and the wrong one.

## Node principals

Refused, but not here: `require_subject` raises `node_principal_not_permitted` before
this handler runs, as it does for every route on the public app (D14, invariant 4). This
module deliberately does not repeat the check — an unreachable branch in a handler reads
like the place the rule lives, and the next person to change the rule would change it
here, where it does nothing.

## It writes no access-log entry

Deliberately, and for the same structural reason an unknown tenant writes none: the log
is tenant-owned, and this answer spans tenants. One entry per tenant inspected would
turn a page load into a dozen rows about a question nobody asks of a log. Invariant 12
is about decisions on resources; this is a question about the shape of the node.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session, col, select

from .auth import require_subject
from .management import ManagementAction, decide_management
from .models import Tenant
from .resources import owning_org
from .subject import Subject


def tenant_router() -> APIRouter:
    router = APIRouter()

    @router.get("/tenants")
    def list_tenants(
        request: Request,
        subject: Subject = Depends(require_subject),
    ) -> list[dict]:
        """The tenants on this node whose organisation you belong to."""
        with Session(request.app.state.engine) as session:
            # `Tenant` is node-global — it is what the tenancy filter keys on, so it
            # cannot itself be filtered by it (R10). The filtering below is by
            # membership, which is a different question from N4's.
            hosted = session.exec(select(Tenant).order_by(col(Tenant.slug))).all()
            return [entry for entry in map(_visible_to(subject), hosted) if entry]

    return router


def _visible_to(subject: Subject) -> Callable[[Tenant], dict | None]:
    """A tenant as this caller may see it, or `None` if they may not see it at all."""

    def seen(tenant: Tenant) -> dict | None:
        org = owning_org(tenant)
        if org is None:
            # A provisioning fault. Skipped rather than raised, because one bad row
            # should not take the listing down for everyone else on the node.
            return None

        if not subject.belongs_to(org):
            return None

        return {
            "slug": tenant.slug,
            "org": org,
            # "may you list its resources" — the lightest management right there is, so
            # it answers "will the next screen work" without implying more.
            "can_manage": decide_management(subject, ManagementAction.RESOURCE_READ, org).allowed,
        }

    return seen
