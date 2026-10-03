"""The decision table (N6, §4.6.2).

`decide()` carries most of the product's security properties, and it is pure — so
covering it exhaustively costs nothing but care. Two kinds of test here, and the
difference matters:

**Written rows** say what *should* happen and why. Each is a claim someone made, and if
one is wrong the test is wrong in a way a reader can see.

**Exhaustive sweeps** assert the absolutes over the whole product of visibility ×
principal type × agreement status × time. They cover combinations nobody enumerated by
hand, and they cannot be vacuous, because what they assert does not depend on the
matrix: a node principal is denied *whatever* the other three are.

Generating the full product with computed expectations was the alternative and is a trap
— the expectations would have to come from a second implementation of `decide()`, and
when the two disagreed there would be no way to tell which was right. This project has
shipped three silently-vacuous tests already; that would have been the fourth.
"""

from __future__ import annotations

import datetime as dt
import itertools
import uuid

import pytest

from circuless_node.decide import Action, decide
from circuless_node.errors import Reason
from circuless_node.models import AgreementCache, Resource
from circuless_node.subject import PrincipalType, Subject
from circuless_node.vocabularies import (
    Classification,
    ResourceKind,
    ResourceStatus,
    Shape,
    Theme,
    Visibility,
)

NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.UTC)
OWNER = "alpha"
CONSUMER = "beta"
TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


# --- the cast ---------------------------------------------------------------------------


def principal(
    principal_type: PrincipalType = PrincipalType.USER,
    *,
    orgs: set[str] | None = None,
    admin_of: set[str] | None = None,
) -> Subject:
    admin = frozenset(admin_of or set())
    return Subject(
        sub="s",
        principal_type=principal_type,
        # Admin implies membership, exactly as the resolver produces it.
        org_ids=frozenset(orgs or set()) | admin,
        admin_of=admin,
        actor=None,
    )


OWNER_ADMIN = principal(admin_of={OWNER})
OWNER_MEMBER = principal(orgs={OWNER})
OWNER_SERVICE = principal(PrincipalType.SERVICE, orgs={OWNER})
CONSUMER_MEMBER = principal(orgs={CONSUMER})
CONSUMER_ADMIN = principal(admin_of={CONSUMER})
CONSUMER_SERVICE = principal(PrincipalType.SERVICE, orgs={CONSUMER})
OUTSIDER = principal(orgs={"gamma"})
NODE = principal(PrincipalType.NODE)
#: In both the consumer organisation and an unrelated one — R11's reason for existing.
CONSULTANT = principal(orgs={CONSUMER, "gamma"})


def dataset(visibility: Visibility, status: ResourceStatus = ResourceStatus.ACTIVE) -> Resource:
    return Resource(
        tenant_id=TENANT,
        slug="batch-7",
        kind=ResourceKind.DATASET,
        shape=Shape.FILE,
        status=status,
        title="Recycled PET batch 7",
        theme=Theme.MATERIAL_CHARACTERISATION,
        classification=Classification.NON_SENSITIVE,
        licence="CC-BY-4.0",
        visibility=visibility,
    )


def service(visibility: Visibility = Visibility.AGREEMENT) -> Resource:
    return Resource(
        tenant_id=TENANT,
        slug="optimiser",
        kind=ResourceKind.SERVICE,
        shape=Shape.SERVICE,
        title="Process optimiser",
        theme=Theme.PROCESSING,
        classification=Classification.NON_SENSITIVE,
        licence="Apache-2.0",
        visibility=visibility,
        endpoint_url="http://optimiser.internal:8080",
    )


def agreement(
    *,
    provider: str = OWNER,
    consumer: str = CONSUMER,
    resource_id: uuid.UUID | None = None,
    actions: str = "read",
    status: str = "accepted",
    valid_from: dt.datetime | None = None,
    valid_until: dt.datetime | None = None,
) -> AgreementCache:
    return AgreementCache(
        id=uuid.uuid4(),
        provider_org=provider,
        consumer_org=consumer,
        resource_id=resource_id,
        actions=actions,
        status=status,
        valid_from=valid_from or (NOW - dt.timedelta(days=1)),
        valid_until=valid_until,
    )


def call(subject, action, resource, agreements=(), now=NOW, requested=None):
    return decide(subject, action, resource, OWNER, agreements, now, requested)


# --- written rows: what each visibility means ---------------------------------------------


@pytest.mark.parametrize(
    ("why", "subject", "visibility", "allowed"),
    [
        # private — admins of the owning org, and nobody else. The distinction from
        # `org` is the whole reason both exist.
        ("private: the owner's admin", OWNER_ADMIN, Visibility.PRIVATE, True),
        ("private: a plain member of the owner", OWNER_MEMBER, Visibility.PRIVATE, False),
        ("private: the owner's service account", OWNER_SERVICE, Visibility.PRIVATE, False),
        ("private: an outsider", OUTSIDER, Visibility.PRIVATE, False),
        # org — any user or service of the owning organisation.
        ("org: the owner's admin", OWNER_ADMIN, Visibility.ORG, True),
        ("org: a plain member", OWNER_MEMBER, Visibility.ORG, True),
        ("org: the owner's service account", OWNER_SERVICE, Visibility.ORG, True),
        ("org: an outsider", OUTSIDER, Visibility.ORG, False),
        ("org: a consumer with no agreement", CONSUMER_MEMBER, Visibility.ORG, False),
        # public — any authenticated principal. Never anonymous: D21 means there is no
        # such caller to test.
        ("public: an outsider", OUTSIDER, Visibility.PUBLIC, True),
        ("public: a service of another org", CONSUMER_SERVICE, Visibility.PUBLIC, True),
        ("public: the owner", OWNER_MEMBER, Visibility.PUBLIC, True),
        # agreement, with none held — the owner still gets in.
        ("agreement: the owner, without one", OWNER_MEMBER, Visibility.AGREEMENT, True),
        ("agreement: an outsider, without one", OUTSIDER, Visibility.AGREEMENT, False),
    ],
)
def test_visibility(why: str, subject: Subject, visibility: Visibility, allowed: bool) -> None:
    assert call(subject, Action.READ, dataset(visibility)).allowed is allowed, why


def test_a_denial_says_which_rule_refused() -> None:
    """The reason code is the contract. `no_agreement` is actionable — ask for one —
    where `not_permitted` is not, and a caller should be able to tell them apart."""
    assert call(OUTSIDER, Action.READ, dataset(Visibility.ORG)).reason is Reason.NOT_PERMITTED
    assert call(OUTSIDER, Action.READ, dataset(Visibility.AGREEMENT)).reason is Reason.NO_AGREEMENT


# --- written rows: what an agreement has to satisfy ------------------------------------------


@pytest.mark.parametrize(
    ("why", "kwargs", "allowed"),
    [
        ("a plain accepted agreement", {}, True),
        ("naming this resource explicitly", {"resource_id": None}, True),
        # Every one of these is a separate reason to exclude an agreement.
        ("granted by a different provider", {"provider": "gamma"}, False),
        ("granted to a different consumer", {"consumer": "gamma"}, False),
        ("still only requested", {"status": "requested"}, False),
        ("offered but never accepted", {"status": "offered"}, False),
        ("rejected", {"status": "rejected"}, False),
        ("revoked", {"status": "revoked"}, False),
        ("expired", {"status": "expired"}, False),
        ("permitting invoke, not read", {"actions": "invoke"}, False),
        ("permitting both", {"actions": "invoke,read"}, True),
    ],
)
def test_agreement_matching(why: str, kwargs: dict, allowed: bool) -> None:
    decision = call(
        CONSUMER_MEMBER, Action.READ, dataset(Visibility.AGREEMENT), [agreement(**kwargs)]
    )
    assert decision.allowed is allowed, why


def test_an_agreement_for_another_resource_does_not_apply() -> None:
    """A node hosts several organisations and caches agreements for all of them. An
    agreement naming one resource must not unlock a sibling."""
    other = uuid.uuid4()
    decision = call(
        CONSUMER_MEMBER,
        Action.READ,
        dataset(Visibility.AGREEMENT),
        [agreement(resource_id=other)],
    )
    assert decision.allowed is False


def test_a_blanket_agreement_covers_every_resource_of_the_provider() -> None:
    """A null `resource_id` means all of them — the shape of a framework arrangement."""
    assert call(
        CONSUMER_MEMBER,
        Action.READ,
        dataset(Visibility.AGREEMENT),
        [agreement(resource_id=None)],
    ).allowed


def test_a_resource_specific_agreement_covers_that_resource() -> None:
    resource = dataset(Visibility.AGREEMENT)
    assert call(
        CONSUMER_MEMBER,
        Action.READ,
        resource,
        [agreement(resource_id=resource.id)],
    ).allowed


# --- written rows: the time boundary, which is half-open -------------------------------------


@pytest.mark.parametrize(
    ("why", "offset", "allowed"),
    [
        ("an instant before it starts", dt.timedelta(microseconds=-1), False),
        ("exactly at valid_from — inclusive", dt.timedelta(0), True),
        ("in the middle", dt.timedelta(hours=12), True),
    ],
)
def test_the_window_starts_inclusively(why: str, offset: dt.timedelta, allowed: bool) -> None:
    start = NOW
    decision = call(
        CONSUMER_MEMBER,
        Action.READ,
        dataset(Visibility.AGREEMENT),
        [agreement(valid_from=start, valid_until=start + dt.timedelta(days=1))],
        now=start + offset,
    )
    assert decision.allowed is allowed, why


@pytest.mark.parametrize(
    ("why", "offset", "allowed"),
    [
        ("an instant before it ends", dt.timedelta(microseconds=-1), True),
        ("exactly at valid_until — exclusive", dt.timedelta(0), False),
        ("after it ends", dt.timedelta(seconds=1), False),
    ],
)
def test_the_window_ends_exclusively(why: str, offset: dt.timedelta, allowed: bool) -> None:
    """`[valid_from, valid_until)`. Half-open so an agreement ending at midnight and one
    starting at midnight do not both apply for an instant, and so `valid_until` reads as
    "until" rather than "through"."""
    end = NOW + dt.timedelta(days=1)
    decision = call(
        CONSUMER_MEMBER,
        Action.READ,
        dataset(Visibility.AGREEMENT),
        [agreement(valid_from=NOW - dt.timedelta(days=1), valid_until=end)],
        now=end + offset,
    )
    assert decision.allowed is allowed, why


def test_no_valid_until_means_no_expiry() -> None:
    assert call(
        CONSUMER_MEMBER,
        Action.READ,
        dataset(Visibility.AGREEMENT),
        [agreement(valid_until=None)],
        now=NOW + dt.timedelta(days=36500),
    ).allowed


# --- written rows: the acting organisation (R11) ----------------------------------------------


def test_one_matching_organisation_needs_no_header() -> None:
    decision = call(CONSUMER_MEMBER, Action.READ, dataset(Visibility.AGREEMENT), [agreement()])
    assert decision.allowed
    assert decision.acting_org == CONSUMER


def test_two_matching_organisations_are_ambiguous() -> None:
    """A consultant in two organisations, both of which hold an agreement. Guessing
    would mean reading one client's data under another client's rights."""
    decision = call(
        CONSULTANT,
        Action.READ,
        dataset(Visibility.AGREEMENT),
        [agreement(consumer=CONSUMER), agreement(consumer="gamma")],
    )
    assert decision.allowed is False
    assert decision.reason is Reason.AMBIGUOUS_ACTING_ORG


def test_the_header_resolves_the_ambiguity() -> None:
    decision = call(
        CONSULTANT,
        Action.READ,
        dataset(Visibility.AGREEMENT),
        [agreement(consumer=CONSUMER), agreement(consumer="gamma")],
        requested="gamma",
    )
    assert decision.allowed
    assert decision.acting_org == "gamma"


def test_naming_an_organisation_you_are_not_in_is_refused_not_ignored() -> None:
    decision = call(
        CONSUMER_MEMBER,
        Action.READ,
        dataset(Visibility.AGREEMENT),
        [agreement()],
        requested="gamma",
    )
    assert decision.allowed is False
    assert decision.reason is Reason.NOT_PERMITTED


def test_the_owner_acts_as_itself() -> None:
    decision = call(OWNER_MEMBER, Action.READ, dataset(Visibility.ORG))
    assert decision.acting_org == OWNER


def test_public_access_names_no_acting_organisation() -> None:
    """Nothing of the caller's is what permits it, so claiming one in the log would be
    an invention — and a multi-org caller is not ambiguous, because there is nothing to
    be ambiguous between."""
    decision = call(CONSULTANT, Action.READ, dataset(Visibility.PUBLIC))
    assert decision.allowed
    assert decision.acting_org is None


# --- written rows: actions and shapes ---------------------------------------------------------


def test_invoke_applies_to_a_service_and_read_does_not() -> None:
    assert call(CONSUMER_MEMBER, Action.INVOKE, service(), [agreement(actions="invoke")]).allowed
    assert call(CONSUMER_MEMBER, Action.READ, service(), [agreement()]).reason is Reason.UNSUPPORTED


def test_read_applies_to_a_dataset_and_invoke_does_not() -> None:
    """A shape error rather than a refusal of permission: answering `not_permitted`
    would suggest the right agreement could make it work."""
    assert (
        call(
            CONSUMER_MEMBER,
            Action.INVOKE,
            dataset(Visibility.AGREEMENT),
            [agreement(actions="invoke")],
        ).reason
        is Reason.UNSUPPORTED
    )


# --- exhaustive sweeps: the absolutes ------------------------------------------------------

VISIBILITIES = list(Visibility)
PRINCIPALS = [OWNER_ADMIN, OWNER_MEMBER, OWNER_SERVICE, CONSUMER_MEMBER, OUTSIDER, CONSULTANT]
STATUSES = ["accepted", "requested", "offered", "rejected", "revoked", "expired"]
MOMENTS = [
    NOW - dt.timedelta(days=2),
    NOW,
    NOW + dt.timedelta(days=2),
]
ACTIONS = list(Action)


@pytest.mark.parametrize(
    ("visibility", "status", "moment", "action"),
    list(itertools.product(VISIBILITIES, STATUSES, MOMENTS, ACTIONS)),
)
def test_a_node_principal_is_denied_in_every_combination(
    visibility: Visibility, status: str, moment: dt.datetime, action: Action
) -> None:
    """D14, swept. A node authenticates as infrastructure and never consumes.

    Exhaustive because this is the rule an attacker reaches by stealing a node's key
    rather than a person's password, and because what it asserts does not depend on any
    of the four dimensions — which is what makes a sweep meaningful rather than noise.
    """
    resource = dataset(visibility) if action is Action.READ else service(visibility)
    decision = call(NODE, action, resource, [agreement(status=status)], now=moment)
    assert decision.allowed is False
    assert decision.reason is Reason.NODE_PRINCIPAL_NOT_PERMITTED


@pytest.mark.parametrize(
    ("visibility", "subject", "status", "moment"),
    list(itertools.product(VISIBILITIES, PRINCIPALS, STATUSES, MOMENTS)),
)
def test_a_withdrawn_resource_is_denied_to_everyone(
    visibility: Visibility, subject: Subject, status: str, moment: dt.datetime
) -> None:
    """D25. Withdrawn means gone — including to the organisation that owns it, and
    including under an agreement that would otherwise permit it."""
    resource = dataset(visibility, status=ResourceStatus.WITHDRAWN)
    decision = call(subject, Action.READ, resource, [agreement(status=status)], now=moment)
    assert decision.allowed is False
    assert decision.reason is Reason.NOT_FOUND


#: Everyone above who is *not* in the owning organisation. The owner is excluded from
#: the agreement sweeps deliberately: they have access by ownership, so an agreement
#: proves nothing about them. Filtered rather than skipped — a skipped security test is
#: one nobody reads, and the count at the end should be cases that ran.
NON_OWNERS = [p for p in PRINCIPALS if not p.belongs_to(OWNER)]


@pytest.mark.parametrize(
    ("subject", "status", "moment"),
    list(itertools.product(NON_OWNERS, [s for s in STATUSES if s != "accepted"], MOMENTS)),
)
def test_only_an_accepted_agreement_ever_permits(
    subject: Subject, status: str, moment: dt.datetime
) -> None:
    """`revoked` and `expired` arrive in the sync feed deliberately, so a denial can be
    explained (R4). They must never grant — and neither must a request nobody answered.
    """
    decision = call(
        subject,
        Action.READ,
        dataset(Visibility.AGREEMENT),
        [agreement(status=status)],
        now=moment,
    )
    assert decision.allowed is False


def test_the_owner_is_allowed_by_ownership_not_by_an_agreement() -> None:
    """The case the sweep above excludes, asserted on its own rather than skipped.

    It also matters operationally: a provider whose sync is stale, or who has no
    agreement with themselves, must still reach their own data.
    """
    assert call(
        OWNER_MEMBER, Action.READ, dataset(Visibility.AGREEMENT), [agreement(status="revoked")]
    ).allowed


@pytest.mark.parametrize(
    ("visibility", "subject", "action"),
    list(itertools.product(VISIBILITIES, PRINCIPALS, ACTIONS)),
)
def test_an_agreement_from_another_provider_never_grants_anything(
    visibility: Visibility, subject: Subject, action: Action
) -> None:
    """A node caches agreements for every organisation it hosts. One tenant's grant must
    never unlock another's data — and only ownership or a matching agreement may allow.
    """
    resource = dataset(visibility) if action is Action.READ else service(visibility)
    # Granted by someone else entirely, to this very caller — the most tempting shape.
    foreign = agreement(
        provider="gamma", consumer=sorted(subject.org_ids)[0], actions="read,invoke"
    )
    decision = call(subject, action, resource, [foreign])

    if decision.allowed:
        # Only two things may allow here, and neither is that agreement.
        assert visibility is Visibility.PUBLIC or subject.belongs_to(OWNER), (
            f"{visibility.value} allowed for {sorted(subject.org_ids)} on an agreement "
            "granted by a different provider"
        )


@pytest.mark.parametrize("visibility", VISIBILITIES)
def test_decide_is_pure(visibility: Visibility) -> None:
    """Same inputs, same answer — twice, with the inputs reused.

    Cheap, and it would catch the most damaging possible regression here: a `decide()`
    that consumed its `agreements` iterable would allow the first call and deny every
    one after it, intermittently, under load.
    """
    resource = dataset(visibility)
    agreements = [agreement()]
    first = call(CONSUMER_MEMBER, Action.READ, resource, agreements)
    second = call(CONSUMER_MEMBER, Action.READ, resource, agreements)
    assert first == second
