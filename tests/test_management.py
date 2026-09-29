"""Who may manage a tenant's resources (N18), as a table.

Pure, so the whole rule set is asserted without a token, a request or a database — the
same argument as the Cloud's `authz.decide()`. These are the cases M5 exercises end to
end; they should have failed here long before then.
"""

from __future__ import annotations

import pytest

from circuless_node.errors import NodeError, Reason
from circuless_node.management import (
    ManagementAction,
    decide_management,
    enforce_management,
)
from circuless_node.subject import PrincipalType, Subject


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
        org_ids=frozenset(orgs or set()) | admin,
        admin_of=admin,
        actor=None,
    )


ALPHA_ADMIN = principal(admin_of={"alpha"})
ALPHA_MEMBER = principal(orgs={"alpha"})
ALPHA_SERVICE = principal(PrincipalType.SERVICE, orgs={"alpha"})
BETA_ADMIN = principal(admin_of={"beta"})
NODE = principal(PrincipalType.NODE)

WRITES = [
    ManagementAction.RESOURCE_REGISTER,
    ManagementAction.RESOURCE_UPDATE,
    ManagementAction.RESOURCE_DELETE,
    ManagementAction.RESOURCE_UPLOAD,
]


@pytest.mark.parametrize("action", WRITES)
def test_an_org_admin_may_write(action: ManagementAction) -> None:
    assert decide_management(ALPHA_ADMIN, action, "alpha").allowed is True


@pytest.mark.parametrize("action", WRITES)
def test_a_service_principal_of_the_org_may_write(action: ManagementAction) -> None:
    """A pipeline publishing yesterday's run is the reason services get this at all."""
    assert decide_management(ALPHA_SERVICE, action, "alpha").allowed is True


@pytest.mark.parametrize("action", WRITES)
def test_a_plain_member_may_not_write(action: ManagementAction) -> None:
    """Membership lets you consume under visibility=org; it does not let you publish on
    the organisation's behalf. The surprising refusal, so it says so."""
    decision = decide_management(ALPHA_MEMBER, action, "alpha")
    assert decision.allowed is False
    assert "admin or a service" in (decision.detail or "")


@pytest.mark.parametrize("action", list(ManagementAction))
def test_another_organisations_admin_may_do_nothing(action: ManagementAction) -> None:
    assert decide_management(BETA_ADMIN, action, "alpha").allowed is False


@pytest.mark.parametrize("action", list(ManagementAction))
def test_a_node_principal_may_do_nothing(action: ManagementAction) -> None:
    """D14, refused a second time.

    N2 already rejects node tokens before a handler sees one. This is the last gate
    before an identity becomes permission, and the reason code stays the specific one so
    a node operator debugging gets told what is actually wrong.
    """
    decision = decide_management(NODE, action, "alpha", node_admin=True)
    assert decision.allowed is False
    assert decision.reason is Reason.NODE_PRINCIPAL_NOT_PERMITTED


def test_credentials_are_admins_only_never_a_service() -> None:
    """N18's whole point.

    A pipeline that can publish is useful. The same pipeline being able to rotate the
    upstream credential means a compromised pipeline can redirect where the node fetches
    from — so the machine gets the narrower right.
    """
    assert decide_management(ALPHA_ADMIN, ManagementAction.CREDENTIAL_SET, "alpha").allowed
    assert (
        decide_management(ALPHA_SERVICE, ManagementAction.CREDENTIAL_SET, "alpha").allowed is False
    )


def test_the_access_log_is_for_that_organisations_admins() -> None:
    assert decide_management(ALPHA_ADMIN, ManagementAction.ACCESS_LOG_READ, "alpha").allowed
    assert (
        decide_management(ALPHA_SERVICE, ManagementAction.ACCESS_LOG_READ, "alpha").allowed is False
    )
    assert decide_management(BETA_ADMIN, ManagementAction.ACCESS_LOG_READ, "alpha").allowed is False


def test_node_configuration_needs_the_node_admin_role() -> None:
    """A role on the node's own client, not an organisation's. An org admin runs their
    organisation; they do not run somebody's server."""
    assert (
        decide_management(
            ALPHA_ADMIN, ManagementAction.NODE_CONFIGURE, "alpha", node_admin=True
        ).allowed
        is True
    )
    assert decide_management(ALPHA_ADMIN, ManagementAction.NODE_CONFIGURE, "alpha").allowed is False


@pytest.mark.parametrize("action", list(ManagementAction))
def test_every_action_has_an_opinion(action: ManagementAction) -> None:
    """No action falls through to an accidental allow.

    A member of no organisation at all is the weakest caller there is; if any action lets
    them through, the table has a hole.
    """
    nobody = principal(orgs=set())
    assert decide_management(nobody, action, "alpha").allowed is False


def test_enforce_raises_403() -> None:
    with pytest.raises(NodeError) as raised:
        enforce_management(ALPHA_MEMBER, ManagementAction.RESOURCE_REGISTER, "alpha")
    assert raised.value.status_code == 403
