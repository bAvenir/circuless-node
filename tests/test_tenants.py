"""`GET /v1/tenants` — the listing an interface needs before it can name anything.

The endpoint exists because a tenant slug cannot be derived from a token, so the thing
most worth proving is the pair of boundaries around it:

1. **You see your organisations and no others.** Not "you see fewer details of others" —
   nothing of another organisation appears at all.
2. **`can_manage` tracks the real management decision**, not membership. The two come
   apart for a plain member, which is exactly the case an interface gets wrong.

Driven by tokens from the real fixture realm rather than hand-built `Subject`s, because
the interesting callers here are the awkward ones the realm already models: an admin who
was never added to the parent group, a user whose groups sit outside `/orgs`, a service
principal, a node.
"""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from starlette.testclient import TestClient

from circuless_node.app import create_public_app
from circuless_node.models import Tenant
from circuless_node.settings import Settings

from .harness.keycloak import FixtureRealm

ALPHA_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
BETA_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000b1")


@pytest.fixture
def node(realm: FixtureRealm, tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        issuer=realm.issuer,
        database_url=f"sqlite:///{tmp_path / 'node.db'}",
        data_dir=tmp_path / "data",
        cors_allow_origins=["https://ui.circuless.eu"],
    )


@pytest.fixture
def app(node: Settings):
    built = create_public_app(node)
    built.state.engine = create_engine(node.database_url)
    SQLModel.metadata.create_all(built.state.engine)
    with Session(built.state.engine) as session:
        # Alpha's tenant slug deliberately differs from its organisation slug. If the
        # handler ever returned the organisation instead, every assertion on "alpha-prod"
        # below would fail — which is the point, since a caller who typed the
        # organisation slug into a path would get a 404.
        #
        # Inserted in reverse slug order, so that SQLite's natural rowid order is not
        # the sorted one. Without this the ordering assertions hold whether or not the
        # query orders anything, which is how a vacuous test gets written.
        session.add(Tenant(org_id=BETA_ORG, group_path="/orgs/beta", slug="beta"))
        session.add(Tenant(org_id=ALPHA_ORG, group_path="/orgs/alpha", slug="alpha-prod"))
        session.commit()
    return built


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def listing(client: TestClient, token: str) -> list[dict]:
    response = client.get("/v1/tenants", headers=bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


def slugs(entries: list[dict]) -> list[str]:
    return [entry["slug"] for entry in entries]


# --- who sees what -------------------------------------------------------------------


def test_a_member_sees_their_own_tenant(client, realm):
    assert listing(client, realm.user_token("alpha.user")) == [
        {"slug": "alpha-prod", "org": "alpha", "can_manage": False}
    ]


def test_a_member_of_one_organisation_sees_nothing_of_the_other(client, realm):
    """The whole disclosure boundary, stated as an absence."""
    body = client.get("/v1/tenants", headers=bearer(realm.user_token("alpha.user"))).text
    assert "beta" not in body


def test_a_member_of_both_sees_both(client, realm):
    """`consultant` is in /orgs/alpha and /orgs/beta."""
    assert slugs(listing(client, realm.user_token("consultant"))) == ["alpha-prod", "beta"]


def test_a_user_in_no_organisation_sees_an_empty_list(client, realm):
    """`stranger`: a 200 with nothing in it, not a 403. There is nothing to refuse."""
    assert listing(client, realm.user_token("stranger")) == []


def test_groups_outside_orgs_are_ignored(client, realm):
    """`odd.groups` is in /orgs/alpha, /orgs/beta/admins/subteam and /elsewhere/alpha.

    Only the first is an organisation (G14). A nested group below `admins` must not
    admit them to Beta, which is the reading that would quietly widen the listing.
    """
    assert slugs(listing(client, realm.user_token("odd.groups"))) == ["alpha-prod"]


def test_the_listing_is_ordered_by_slug(client, realm):
    """Stable order, so an interface is not reshuffled between loads.

    The fixture inserts Beta first, so this fails if the query returns rows in
    whatever order the database happens to hold them.
    """
    assert slugs(listing(client, realm.user_token("consultant"))) == ["alpha-prod", "beta"]


# --- can_manage ----------------------------------------------------------------------


def test_an_org_admin_can_manage(client, realm):
    [alpha] = listing(client, realm.user_token("alpha.admin"))
    assert alpha["can_manage"] is True


def test_a_plain_member_cannot_manage(client, realm):
    """The case that makes the flag worth returning: visible, but not writable."""
    [alpha] = listing(client, realm.user_token("alpha.user"))
    assert alpha["can_manage"] is False


def test_an_admin_who_is_not_in_the_parent_group_still_sees_their_tenant(client, realm):
    """`admins.only` is in /orgs/beta/admins but not /orgs/beta.

    An admin of an organisation is a member of it whatever the operator clicked, so
    this must be a listing of Beta and not an empty one.
    """
    assert listing(client, realm.user_token("admins.only")) == [
        {"slug": "beta", "org": "beta", "can_manage": True}
    ]


def test_a_service_principal_sees_and_can_manage_its_organisation(client, realm):
    """`test-service` carries org_id=alpha and no groups at all."""
    assert listing(client, realm.service_token()) == [
        {"slug": "alpha-prod", "org": "alpha", "can_manage": True}
    ]


# --- refusals ------------------------------------------------------------------------


def test_a_node_principal_is_refused(client, realm):
    """D14, invariant 4. Refused by `require_subject`, before the handler runs."""
    response = client.get("/v1/tenants", headers=bearer(realm.node_token()))
    assert response.status_code == 403
    assert response.json()["reason"] == "node_principal_not_permitted"


def test_no_token_is_refused(client):
    """Invariant 2. Also covered by the route enumeration; asserted here too because
    this is the first route whose answer is a list rather than a tenant's own data,
    and "harmless enough to leave open" is the argument that would exempt it."""
    assert client.get("/v1/tenants").status_code == 401


# --- provisioning faults -------------------------------------------------------------


def test_a_tenant_with_an_unresolvable_group_path_is_skipped(client, app, realm):
    """One bad row must not take the listing down for everyone else on the node."""
    with Session(app.state.engine) as session:
        session.add(
            Tenant(
                org_id=uuid.uuid4(),
                group_path="/misprovisioned",
                # Sorts before "alpha-prod", so it is reached first: a row that
                # raised would do so before any good row had been appended.
                slug="aaa-broken",
            )
        )
        session.commit()

    assert slugs(listing(client, realm.user_token("alpha.user"))) == ["alpha-prod"]


def test_an_empty_node_lists_nothing(client, app, realm):
    """No tenants at all is a 200 and an empty list, not an error."""
    with Session(app.state.engine) as session:
        for tenant in session.exec(select(Tenant)).all():
            session.delete(tenant)
        session.commit()

    assert listing(client, realm.user_token("alpha.admin")) == []
