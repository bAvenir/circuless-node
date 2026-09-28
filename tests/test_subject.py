"""Subject resolution and acting-org rules (N3).

Pure functions, so these are table tests with no Keycloak involved. The end-to-end
counterparts — the same rules applied to tokens a real issuer minted — are in
`test_subject_integration.py`; both matter, because a rule that is right in isolation and
wrong about what Keycloak actually emits is still wrong.
"""

from __future__ import annotations

import pytest

from circuless_node.errors import NodeError, Reason
from circuless_node.subject import (
    PrincipalType,
    Subject,
    organisations_from_groups,
    resolve_acting_org,
)


def a_subject(*, orgs: set[str], admin_of: set[str] = frozenset()) -> Subject:
    return Subject(
        sub="00000000-0000-0000-0000-000000000000",
        principal_type=PrincipalType.USER,
        org_ids=frozenset(orgs),
        admin_of=frozenset(admin_of),
        actor="test-ui",
    )


# ------------------------------------------------------------ reading groups (G14)


@pytest.mark.parametrize(
    ("groups", "expected_orgs", "expected_admin_of"),
    [
        pytest.param(["/orgs/alpha"], {"alpha"}, set(), id="a plain member"),
        pytest.param(
            ["/orgs/alpha", "/orgs/alpha/admins"], {"alpha"}, {"alpha"}, id="member and admin"
        ),
        pytest.param(
            ["/orgs/alpha/admins"],
            {"alpha"},
            {"alpha"},
            id="admin implies membership, however the groups were clicked",
        ),
        pytest.param(
            ["/orgs/alpha", "/orgs/beta"], {"alpha", "beta"}, set(), id="two organisations"
        ),
        pytest.param(
            ["/orgs/alpha", "/orgs/beta/admins"],
            {"alpha", "beta"},
            {"beta"},
            id="member of one, admin of another",
        ),
        pytest.param([], set(), set(), id="no groups at all"),
        pytest.param(["/orgs"], set(), set(), id="the container is not an organisation"),
        pytest.param(["/elsewhere/alpha"], set(), set(), id="a group outside /orgs is invisible"),
        pytest.param(
            ["/orgs/alpha/teams/blue"], set(), set(), id="deeper than an org, and not admins"
        ),
        pytest.param(
            ["/orgs/alpha/admins/subteam"],
            set(),
            set(),
            id="deeper than the admins marker confers nothing",
        ),
        pytest.param(
            ["/orgs/alpha/notadmins"],
            set(),
            set(),
            id="an unrecognised subgroup confers nothing, not even membership",
        ),
    ],
)
def test_organisations_are_read_from_depth_one_groups(
    groups: list[str], expected_orgs: set[str], expected_admin_of: set[str]
) -> None:
    org_ids, admin_of = organisations_from_groups(groups)
    assert org_ids == expected_orgs
    assert admin_of == expected_admin_of


def test_admin_of_one_org_is_not_admin_of_another() -> None:
    """R17, from the other side. The realm has no global org-admin role, and the resolver
    must not manufacture one."""
    _, admin_of = organisations_from_groups(["/orgs/alpha/admins", "/orgs/beta"])
    assert admin_of == {"alpha"}
    assert "beta" not in admin_of


# ------------------------------------------------------------ acting org (R11)


def test_one_eligible_org_needs_no_header() -> None:
    subject = a_subject(orgs={"alpha"})
    assert resolve_acting_org(subject, ["alpha", "beta"]) == "alpha"


def test_several_eligible_orgs_are_ambiguous_without_a_header() -> None:
    """The consultant case. Reading one client's data under another client's rights is
    exactly what this prevents."""
    subject = a_subject(orgs={"alpha", "beta"})

    with pytest.raises(NodeError) as raised:
        resolve_acting_org(subject, ["alpha", "beta"])

    assert raised.value.reason == Reason.AMBIGUOUS_ACTING_ORG
    assert raised.value.status_code == 403


def test_a_header_resolves_the_ambiguity() -> None:
    subject = a_subject(orgs={"alpha", "beta"})
    assert resolve_acting_org(subject, ["alpha", "beta"], requested="beta") == "beta"


def test_naming_an_org_you_do_not_belong_to_is_refused() -> None:
    """The header grants nothing. Refused rather than ignored: quietly acting as a
    different organisation than the caller asked for is worse than an error, because they
    would never find out."""
    subject = a_subject(orgs={"alpha"})

    with pytest.raises(NodeError) as raised:
        resolve_acting_org(subject, ["alpha", "beta"], requested="beta")

    assert raised.value.reason == Reason.NOT_PERMITTED


def test_naming_an_org_that_cannot_authorise_this_request_is_refused() -> None:
    """A member of both, asking to act as the one with no claim on this resource."""
    subject = a_subject(orgs={"alpha", "beta"})

    with pytest.raises(NodeError) as raised:
        resolve_acting_org(subject, ["alpha"], requested="beta")

    assert raised.value.reason == Reason.NOT_PERMITTED


def test_no_overlap_at_all_is_refused() -> None:
    subject = a_subject(orgs={"gamma"})

    with pytest.raises(NodeError) as raised:
        resolve_acting_org(subject, ["alpha", "beta"])

    assert raised.value.reason == Reason.NOT_PERMITTED


def test_membership_in_many_orgs_is_unambiguous_when_only_one_can_authorise() -> None:
    """Ambiguity is about the *intersection*, not about how many organisations someone
    belongs to — otherwise the consultant would need a header on every request."""
    subject = a_subject(orgs={"alpha", "beta", "gamma"})
    assert resolve_acting_org(subject, ["beta"]) == "beta"
