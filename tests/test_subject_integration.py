"""The resolver against tokens a real Keycloak minted (N3).

`test_subject.py` checks the rules in isolation. This checks the rules against what
Keycloak actually puts in a token, which is the half that catches a resolver that is
internally consistent and wrong about reality.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from circuless_node.app import create_public_app
from circuless_node.errors import Reason
from circuless_node.settings import Settings
from circuless_node.subject import ACTING_ORG_HEADER

from .harness.keycloak import FixtureRealm


@pytest.fixture
def client(node_settings: Settings) -> TestClient:
    return TestClient(create_public_app(node_settings), raise_server_exceptions=False)


def whoami(client: TestClient, token: str, acting_org: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if acting_org is not None:
        headers[ACTING_ORG_HEADER] = acting_org
    response = client.get("/v1/whoami", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_single_org_user_resolves_to_that_org(client: TestClient, realm: FixtureRealm) -> None:
    body = whoami(client, realm.user_token("alpha.user"))

    assert body["principal_type"] == "user"
    assert body["org_ids"] == ["alpha"]
    assert body["admin_of"] == []
    assert body["acting_org"] == "alpha", "one organisation needs no header"


def test_an_admin_is_admin_of_their_org_only(client: TestClient, realm: FixtureRealm) -> None:
    body = whoami(client, realm.user_token("alpha.admin"))

    assert body["org_ids"] == ["alpha"]
    assert body["admin_of"] == ["alpha"]


def test_membership_of_the_admins_group_alone_still_makes_a_member(
    client: TestClient, realm: FixtureRealm
) -> None:
    """`admins.only` is in /orgs/beta/admins and not /orgs/beta. Keycloak does not require
    membership of the parent group, so admin rights must not depend on how carefully
    someone clicked."""
    body = whoami(client, realm.user_token("admins.only"))

    assert body["org_ids"] == ["beta"]
    assert body["admin_of"] == ["beta"]


def test_groups_outside_orgs_are_ignored(client: TestClient, realm: FixtureRealm) -> None:
    """G14. `odd.groups` is in /orgs/alpha, /orgs/beta/admins/subteam and
    /elsewhere/alpha. Only the first is an organisation — otherwise any group in the realm
    would confer membership, and the deeper one would confer admin of beta."""
    body = whoami(client, realm.user_token("odd.groups"))

    assert body["org_ids"] == ["alpha"]
    assert body["admin_of"] == [], "a group below the admins marker confers nothing"


def test_a_user_in_no_org_can_act_for_nobody(client: TestClient, realm: FixtureRealm) -> None:
    """A self-registered user nobody has admitted yet (FR1)."""
    body = whoami(client, realm.user_token("stranger"))

    assert body["org_ids"] == []
    assert body["acting_org"] is None
    assert body["acting_org_reason"] == Reason.NOT_PERMITTED


def test_a_service_takes_its_org_from_the_claim_and_is_never_an_admin(
    client: TestClient, realm: FixtureRealm
) -> None:
    body = whoami(client, realm.service_token())

    assert body["principal_type"] == "service"
    assert body["org_ids"] == ["alpha"]
    assert body["admin_of"] == [], "N18: credentials are for admins only, never a service"
    assert body["acting_org"] == "alpha"


# --------------------------------------------------------- the multi-org case (R11)


def test_a_consultant_in_two_orgs_is_ambiguous(client: TestClient, realm: FixtureRealm) -> None:
    body = whoami(client, realm.user_token("consultant"))

    assert body["org_ids"] == ["alpha", "beta"]
    assert body["acting_org"] is None
    assert body["acting_org_reason"] == Reason.AMBIGUOUS_ACTING_ORG


@pytest.mark.parametrize("org", ["alpha", "beta"])
def test_a_consultant_can_name_either_of_their_orgs(
    client: TestClient, realm: FixtureRealm, org: str
) -> None:
    body = whoami(client, realm.user_token("consultant"), acting_org=org)
    assert body["acting_org"] == org


def test_a_consultant_cannot_name_an_org_they_are_not_in(
    client: TestClient, realm: FixtureRealm
) -> None:
    """The header is checked against membership, so it can only ever narrow a choice the
    subject already had — it never grants one."""
    response = client.get(
        "/v1/whoami",
        headers={
            "Authorization": f"Bearer {realm.user_token('consultant')}",
            ACTING_ORG_HEADER: "gamma",
        },
    )

    assert response.json()["acting_org_reason"] == Reason.NOT_PERMITTED


def test_a_single_org_user_cannot_borrow_another_org_with_the_header(
    client: TestClient, realm: FixtureRealm
) -> None:
    """The header as a privilege-escalation attempt: alpha.user claiming to act as beta."""
    response = client.get(
        "/v1/whoami",
        headers={
            "Authorization": f"Bearer {realm.user_token('alpha.user')}",
            ACTING_ORG_HEADER: "beta",
        },
    )

    assert response.json()["acting_org_reason"] == Reason.NOT_PERMITTED


def test_a_node_token_is_still_refused_after_resolution(
    client: TestClient, realm: FixtureRealm
) -> None:
    """N2 already refuses it; the resolver refuses it again. D14 is worth failing twice,
    because resolution is the last point before a caller becomes an identity."""
    response = client.get("/v1/whoami", headers={"Authorization": f"Bearer {realm.node_token()}"})

    assert response.status_code == 403
    assert response.json()["reason"] == Reason.NODE_PRINCIPAL_NOT_PERMITTED
