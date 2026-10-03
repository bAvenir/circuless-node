"""Who is asking, and on whose behalf (N3).

One resolver produces one `Subject`, so `decide()` never has to know which Keycloak mapper
filled which claim (§3.2). Everything here comes from the token and nothing from the
request body — a body field would let anyone act as another organisation (R3, D17). The
one thing the caller may say is *which* of their own organisations they are acting as, and
even that is checked against membership.

Three rules do most of the work:

* **Organisations are depth one under `/orgs`.** `/orgs/alpha` is an organisation;
  `/orgs/alpha/admins` makes someone its admin; anything deeper, and anything outside
  `/orgs`, is ignored (G14). Without that, any group in the realm becomes an organisation.
* **A service's organisation comes from its `org_id` claim, never from groups**, and a
  service is never an org admin — N18 reserves credential rotation for admins precisely so
  that a compromised pipeline cannot redirect upstream credentials.
* **`org_ids` is a set** (R11). People work for more than one organisation, and which one
  they are acting as has to be resolved per request rather than assumed.

## A note on what an organisation is called

Today these are slugs: `alpha` from `/orgs/alpha` for a user, and whatever the `org_id`
attribute holds for a service. §3.2 says the Cloud's registry maps `/orgs/alpha` to a UUID
and syncs it to nodes, at which point both sides should carry UUIDs instead.

**Both sides have to move together.** If users resolved to slugs while services resolved to
UUIDs, every comparison between them would silently fail to match — a service would simply
never be in the same organisation as a user. The translation belongs in N7, where OrgMap
arrives, and it must cover both sources at once.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .errors import NodeError, Reason

ORGS_ROOT = "orgs"
ADMINS_SUBGROUP = "admins"

#: The caller names which of their own organisations they are acting as. Checked against
#: membership, so it grants nothing — it only disambiguates.
ACTING_ORG_HEADER = "X-CIRCULess-Acting-Org"


class PrincipalType(StrEnum):
    USER = "user"
    SERVICE = "service"
    NODE = "node"


@dataclass(frozen=True)
class Subject:
    """The caller, normalised. `decide()` takes this and nothing else about identity."""

    sub: str
    principal_type: PrincipalType
    org_ids: frozenset[str]
    admin_of: frozenset[str]
    actor: str | None

    def belongs_to(self, org_id: str) -> bool:
        return org_id in self.org_ids

    def is_admin_of(self, org_id: str) -> bool:
        return org_id in self.admin_of


def resolve_subject(claims) -> Subject:  # noqa: ANN001 — a VerifiedToken
    principal_type = _principal_type(claims.principal_type)

    if principal_type is PrincipalType.NODE:
        # N2 already refused this. Repeated here because resolution is the last point
        # before a caller becomes an identity, and D14 is worth failing twice.
        raise NodeError(
            403,
            Reason.NODE_PRINCIPAL_NOT_PERMITTED,
            "a node principal may not consume or manage resources",
        )

    if principal_type is PrincipalType.SERVICE:
        org_id = claims.org_id
        if not org_id:
            raise NodeError(401, Reason.INVALID_TOKEN, "a service principal carries no org_id")
        # Groups are ignored entirely, and admin_of is always empty: a service account
        # dropped into /orgs/x/admins by mistake must not become an org admin (N18).
        return Subject(
            sub=claims.sub,
            principal_type=principal_type,
            org_ids=frozenset({org_id}),
            admin_of=frozenset(),
            actor=claims.actor,
        )

    org_ids, admin_of = organisations_from_groups(claims.groups)
    return Subject(
        sub=claims.sub,
        principal_type=principal_type,
        org_ids=org_ids,
        admin_of=admin_of,
        actor=claims.actor,
    )


def _principal_type(value: str) -> PrincipalType:
    try:
        return PrincipalType(value)
    except ValueError:
        # An unrecognised type is refused rather than guessed at, for the same reason a
        # missing one is (N2): guessing turns a misconfiguration into an authorisation.
        raise NodeError(401, Reason.INVALID_TOKEN, "unrecognised principal_type") from None


def organisations_from_groups(groups: Iterable[str]) -> tuple[frozenset[str], frozenset[str]]:
    """Read `/orgs/<x>` and `/orgs/<x>/admins` out of the group paths, ignoring the rest.

    Everything else in the realm — other group trees, and anything nested below an
    organisation — is deliberately invisible here (G14).
    """
    org_ids: set[str] = set()
    admin_of: set[str] = set()

    for path in groups:
        parts = [part for part in path.split("/") if part]
        # `/orgs` itself is the container, not an organisation.
        if len(parts) < 2 or parts[0] != ORGS_ROOT:
            continue

        org_id = parts[1]
        if len(parts) == 2:
            org_ids.add(org_id)
        elif len(parts) == 3 and parts[2] == ADMINS_SUBGROUP:
            admin_of.add(org_id)
            # An admin of an organisation is a member of it. Keycloak does not require
            # membership of the parent group, so relying on the operator to have added
            # both would make admin rights depend on how carefully someone clicked.
            org_ids.add(org_id)
        # Anything deeper — /orgs/x/admins/subteam, /orgs/x/teams/y — is ignored.

    return frozenset(org_ids), frozenset(admin_of)


@dataclass(frozen=True)
class ActingOrg:
    """Which organisation a request is made on behalf of."""

    org: str


@dataclass(frozen=True)
class ActingOrgRefused:
    """No organisation of theirs can authorise it, or they named one they may not use."""

    reason: Reason
    detail: str


def acting_org(
    subject: Subject,
    candidates: Iterable[str],
    requested: str | None = None,
) -> ActingOrg | ActingOrgRefused:
    """Decide which organisation this request is made on behalf of (R11). Pure.

    `candidates` are the organisations that could authorise *this* request — the
    resource's owner, or the consumer side of a matching agreement. Intersecting them
    with the subject's own organisations is what keeps a multi-org person from reading
    one client's data under another client's rights.

    Auditors ask on whose behalf a consultant read a file. This is the answer, and N11
    logs it.

    **Returns rather than raises**, because `decide()` is pure and a refusal is an
    ordinary outcome there, not an exception. `resolve_acting_org` below is the raising
    wrapper for the handlers that want one. One implementation of R11, two shapes — if
    they were written separately, the rule that a header naming someone else's
    organisation is *refused rather than ignored* would eventually hold in only one.
    """
    eligible = subject.org_ids & frozenset(candidates)

    if requested is not None:
        if not subject.belongs_to(requested):
            # Naming someone else's organisation is refused outright rather than
            # ignored: silently acting as a different org than the caller asked for is
            # worse than an error, because they would never find out.
            return ActingOrgRefused(
                Reason.NOT_PERMITTED, "not a member of the requested acting organisation"
            )
        if requested not in eligible:
            return ActingOrgRefused(
                Reason.NOT_PERMITTED,
                "the requested acting organisation cannot authorise this request",
            )
        return ActingOrg(requested)

    if len(eligible) == 1:
        return ActingOrg(next(iter(eligible)))

    if not eligible:
        return ActingOrgRefused(
            Reason.NOT_PERMITTED, "no organisation of yours can authorise this request"
        )

    return ActingOrgRefused(
        Reason.AMBIGUOUS_ACTING_ORG,
        f"several of your organisations could authorise this; send {ACTING_ORG_HEADER}",
    )


def resolve_acting_org(
    subject: Subject,
    candidates: Iterable[str],
    requested: str | None = None,
) -> str:
    """`acting_org`, raising. For handlers that want an exception rather than a result.

    The behaviour and the reason codes are `acting_org`'s; this only changes the shape.
    """
    result = acting_org(subject, candidates, requested)
    if isinstance(result, ActingOrgRefused):
        raise NodeError(403, result.reason, result.detail)
    return result.org
