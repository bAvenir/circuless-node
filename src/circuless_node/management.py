"""Who may manage a tenant's resources (N18).

Pure, like the Cloud's `authz.decide()` and for the same reason: the whole table can be
read in one place and tested as a table, without a token or a request. `decide()` (N6)
is the other half and answers a different question — this one is about *managing*
resources, that one about *consuming* them.

## The table (CLAUDE.md, "Management rules")

| Operation | Who |
|---|---|
| Register, update, upload, delete | admins **or service principals** of the tenant's org |
| Credentials | admins only |
| Read the access log | admins of the tenant's org, and `platform-admin` |
| Node configuration | the node client role `admin` |

## Two things the table is saying carefully

**A service principal may register and upload, but never touch credentials.** That is the
point of N18 existing at all: a pipeline account that can publish yesterday's run is
useful, and the same account being able to rotate the upstream credential means a
compromised pipeline can redirect where the node fetches from. The narrower right is the
one a machine gets.

**Membership is not enough for anything.** Every operation here needs admin or service —
being an ordinary member of an organisation lets you consume under `visibility=org`, not
publish on the organisation's behalf.

## Scoping is not authorisation

N4 guarantees a query about tenant A returns only tenant A's rows. It says nothing about
whether this caller may act on tenant A at all — that is this module. A caller from
another organisation reaching `/v1/t/alpha/...` gets correctly-scoped Alpha data unless
something here stops them, which is exactly the hole the tenancy docstring warns about.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto

from .errors import NodeError, Reason
from .subject import PrincipalType, Subject


class ManagementAction(StrEnum):
    """Everything a caller can ask to do *to* a tenant's resources, rather than with them."""

    RESOURCE_REGISTER = auto()
    RESOURCE_UPDATE = auto()
    RESOURCE_READ = auto()
    RESOURCE_DELETE = auto()
    RESOURCE_UPLOAD = auto()
    CREDENTIAL_SET = auto()
    ACCESS_LOG_READ = auto()
    NODE_CONFIGURE = auto()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: Reason | None = None
    detail: str | None = None

    @staticmethod
    def yes() -> Decision:
        return Decision(True)

    @staticmethod
    def no(detail: str, reason: Reason = Reason.NOT_PERMITTED) -> Decision:
        return Decision(False, reason, detail)


def decide_management(
    subject: Subject,
    action: ManagementAction,
    tenant_org: str,
    *,
    node_admin: bool = False,
) -> Decision:
    """May this subject perform this management action on this tenant's resources?

    `tenant_org` is the slug of the organisation that owns the tenant — resolved by the
    caller from the tenant in the path, never taken from a request body (R3).

    `node_admin` is the node client role, which is about the node itself rather than
    about any organisation, so it arrives separately rather than being inferred.
    """
    # N2 refuses these before a request reaches a handler (D14). Repeated because this is
    # the last gate before an identity becomes permission, and D14 is worth failing twice.
    if subject.principal_type is PrincipalType.NODE:
        return Decision.no(
            "a node principal may not manage resources",
            Reason.NODE_PRINCIPAL_NOT_PERMITTED,
        )

    if action is ManagementAction.NODE_CONFIGURE:
        if not node_admin:
            return Decision.no("configuring the node needs the node's admin client role")
        return Decision.yes()

    if action is ManagementAction.ACCESS_LOG_READ:
        # `platform-admin` is not read by the node today: node tokens carry realm roles,
        # but nothing on this side consumes them yet, and N11 is where the log arrives.
        # Until then the rule is the org-admin half, which is the restrictive half.
        if subject.is_admin_of(tenant_org):
            return Decision.yes()
        return Decision.no("only that organisation's admins may read its access log")

    if action is ManagementAction.CREDENTIAL_SET:
        # Admins only, never a service. A compromised pipeline that could rotate the
        # upstream credential could redirect where this node fetches from.
        if subject.is_admin_of(tenant_org):
            return Decision.yes()
        return Decision.no("only an organisation's admins may set credentials")

    # Register, update, read, delete, upload: admins or service principals of the org.
    if subject.is_admin_of(tenant_org):
        return Decision.yes()
    if subject.principal_type is PrincipalType.SERVICE and subject.belongs_to(tenant_org):
        return Decision.yes()

    if subject.belongs_to(tenant_org):
        # A member, but not an admin and not a service. Said explicitly, because "you are
        # in this organisation but may not publish for it" is the one refusal here that
        # is genuinely surprising.
        return Decision.no("managing resources needs an admin or a service principal")
    return Decision.no("not a member of the organisation that owns this tenant")


def enforce_management(
    subject: Subject,
    action: ManagementAction,
    tenant_org: str,
    *,
    node_admin: bool = False,
) -> None:
    """`decide_management`, raising."""
    decision = decide_management(subject, action, tenant_org, node_admin=node_admin)
    if not decision.allowed:
        raise NodeError(403, decision.reason or Reason.NOT_PERMITTED, decision.detail)
