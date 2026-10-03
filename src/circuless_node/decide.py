"""`decide()` — may this caller do this, to this resource, right now (N6, D1, D2).

The node decides locally. No call to the Cloud on the request path (§3.7): agreements
arrive through the sync client (N7) and are enforced from cache, so a control-plane
outage does not become a data-plane one (F16).

## Pure, and why that is the whole design

No database, no clock, no request object. Agreements and `now` are **inputs**, loaded by
the caller. That is what lets the entire decision table be tested without a server, a
token or a migration — and this function carries most of the product's security
properties, so it is the one place where exhaustive testing has to be cheap.

It also means `decide()` cannot be wrong about *when* it is: a caller who passes a stale
`now` gets a stale answer, and that is the caller's bug rather than a hidden one here.

## The rules (§4.6.2)

| `visibility` | allowed |
|---|---|
| anything **withdrawn** | nobody (D25) |
| `private` | admins of the owning organisation only — not ordinary members |
| `org` | any user or service of the owning organisation |
| `agreement` | the owner, plus any organisation with an **accepted** agreement — see below |
| `public` | any authenticated user or service. **Never anonymous, never a node** |

An `agreement` match needs all of: the agreement's provider owns this resource, its
status is `accepted`, it names this resource or none at all, it permits this action, and
`now` falls inside `[valid_from, valid_until)`.

Refused before any of that: a node principal (D14). N2 already rejects node tokens
before a handler sees one; this is the second lock, because D14 is the rule an attacker
reaches by stealing a node's key rather than a person's password.

## What it does not do

**It does not authenticate.** `aud`, `iss`, expiry and signature are N2's, and a
`Subject` only exists because they passed.

**It does not scope queries.** N4 guarantees a query about tenant A returns only tenant
A's rows; that is a different guarantee, and conflating the two is how a hole appears.

**It does not decide management.** Registering, updating and credentials are N18's
`decide_management`. This is consumption only: reading bytes and invoking services.

**It does not log.** N11 writes the AccessLog, from what this returns — including
`acting_org`, which is the answer to "on whose behalf did this person read that file".
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .errors import Reason
from .models import AgreementCache, Resource
from .subject import ActingOrgRefused, PrincipalType, Subject, acting_org
from .vocabularies import ResourceKind, ResourceStatus, Visibility


class Action(StrEnum):
    """What a consumer can ask to do. An agreement permits a subset of these."""

    #: `GET /data` and `GET /data/{path}` — a dataset's bytes, or one bucket object.
    READ = "read"
    #: `ANY /invoke/{path}` — a service call, proxied by N9.
    INVOKE = "invoke"


#: Which kind of resource each action applies to. A dataset has no `/invoke` and a
#: service has no `/data`, so the mismatch is a shape error rather than a refusal of
#: permission — and saying so stops it being read as "you are not allowed".
ACTION_KIND = {Action.READ: ResourceKind.DATASET, Action.INVOKE: ResourceKind.SERVICE}


@dataclass(frozen=True)
class Decision:
    """Allow or deny, why, and on whose behalf.

    `acting_org` is set on an allow and is what N11 records. It is `None` only for
    `public` access, where no organisation of the caller's is what permits it.
    """

    allowed: bool
    reason: Reason | None = None
    detail: str | None = None
    acting_org: str | None = None

    @staticmethod
    def allow(acting_org: str | None = None) -> Decision:
        return Decision(True, acting_org=acting_org)

    @staticmethod
    def deny(reason: Reason, detail: str) -> Decision:
        return Decision(False, reason, detail)


def decide(
    subject: Subject,
    action: Action,
    resource: Resource,
    owner_org: str,
    agreements: Iterable[AgreementCache],
    now: dt.datetime,
    requested_acting_org: str | None = None,
) -> Decision:
    """May this subject perform this action on this resource at this moment?

    `owner_org` is the slug of the organisation whose tenant owns the resource. It is a
    parameter rather than read from `resource`, because a `Resource` carries a
    `tenant_id` and resolving that to an organisation is a query — which this function
    does not do.

    `requested_acting_org` is `X-CIRCULess-Acting-Org`, and grants nothing: it is checked
    against membership, so it can only narrow a choice the subject already had (R11).
    """
    # D14. N2 refused this already; repeated because this is the last gate before an
    # identity becomes access, and it is the rule that matters most if a node's key
    # leaks.
    if subject.principal_type is PrincipalType.NODE:
        return Decision.deny(
            Reason.NODE_PRINCIPAL_NOT_PERMITTED,
            "a node principal may not consume resources",
        )

    # D25. Withdrawn means gone, to everyone — including the organisation that owns it.
    # `not_found` rather than `not_permitted`: a resource that no longer exists should
    # not be distinguishable from one that never did.
    if resource.status is not ResourceStatus.ACTIVE:
        return Decision.deny(Reason.NOT_FOUND, "no such resource")

    if resource.kind is not ACTION_KIND[action]:
        # A shape error, not a permission one. `read` on a service and `invoke` on a
        # dataset are both nonsense; answering "not permitted" would suggest that the
        # right agreement could make them work.
        return Decision.deny(
            Reason.UNSUPPORTED,
            f"{action.value} does not apply to a {resource.kind.value}",
        )

    if resource.visibility is Visibility.PUBLIC:
        # Any authenticated principal. Never anonymous — D21 means there is no such
        # caller — and never a node, which was refused above.
        #
        # No acting organisation: nothing of the caller's is what permits this, so
        # claiming one in the log would be an invention. A multi-org caller is not
        # ambiguous here, because there is nothing to be ambiguous between.
        return Decision.allow()

    if resource.visibility is Visibility.PRIVATE:
        # Administrators of the owning organisation, and nobody else. Deliberately
        # narrower than `org`: a plain member cannot read it, which is the distinction
        # between the two and the reason both exist.
        if subject.is_admin_of(owner_org):
            return Decision.allow(owner_org)
        return Decision.deny(
            Reason.NOT_PERMITTED, "only the owning organisation's admins may read this"
        )

    if resource.visibility is Visibility.ORG:
        if subject.belongs_to(owner_org):
            return Decision.allow(owner_org)
        return Decision.deny(Reason.NOT_PERMITTED, "only the owning organisation may read this")

    # Visibility.AGREEMENT — the only case where the agreement cache is consulted.
    #
    # The owner always has access to their own resource. Checked first so that a
    # provider never needs an agreement with themselves, and so that a provider whose
    # sync is stale can still reach their own data.
    if subject.belongs_to(owner_org):
        return Decision.allow(owner_org)

    consumers = _consumers_permitted(action, resource, owner_org, agreements, now)
    if not consumers:
        # Distinct from `not_permitted`: there is no agreement, which is actionable —
        # the caller can request one — where "not permitted" is not.
        return Decision.deny(Reason.NO_AGREEMENT, "no accepted agreement covers this")

    result = acting_org(subject, consumers, requested_acting_org)
    if isinstance(result, ActingOrgRefused):
        # Includes `ambiguous_acting_org` when several of the caller's organisations
        # hold an agreement covering this — R11. Guessing one would mean reading a
        # client's data under another client's rights.
        return Decision.deny(result.reason, result.detail)
    return Decision.allow(result.org)


def _consumers_permitted(
    action: Action,
    resource: Resource,
    owner_org: str,
    agreements: Iterable[AgreementCache],
    now: dt.datetime,
) -> set[str]:
    """Organisations an accepted, current agreement lets perform `action` here.

    Every condition is a separate reason to exclude an agreement, and each has bitten
    somewhere in this design:

    * **the provider must own this resource** — a cached agreement names a provider, and
      the node hosts several organisations; without this check an agreement granted by
      one tenant would unlock another's data;
    * **`accepted`, exactly** — `revoked` and `expired` arrive in the sync feed on
      purpose, so that a denial can be explained rather than shrugged at (R4). They must
      never permit;
    * **this resource, or all of the provider's** — a null `resource_id` is a blanket
      agreement;
    * **the action** — `read` and `invoke` are granted separately;
    * **the window**, half-open: `valid_from <= now < valid_until`, with a null
      `valid_until` meaning no expiry.
    """
    permitted: set[str] = set()
    for agreement in agreements:
        if agreement.provider_org != owner_org:
            continue
        if agreement.status != "accepted":
            continue
        if agreement.resource_id is not None and agreement.resource_id != resource.id:
            continue
        if not agreement.permits(action.value):
            continue
        if not _within(agreement, now):
            continue
        permitted.add(agreement.consumer_org)
    return permitted


def _within(agreement: AgreementCache, now: dt.datetime) -> bool:
    """`[valid_from, valid_until)` — inclusive start, exclusive end.

    Half-open so that an agreement ending at midnight and one starting at midnight do
    not both apply for an instant, and so that `valid_until` reads as "until", not
    "through".
    """
    if now < agreement.valid_from:
        return False
    return agreement.valid_until is None or now < agreement.valid_until
