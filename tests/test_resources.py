"""The resource registry (N5, F3, F4), against a real issuer.

Tokens come from the fixture realm rather than being minted here — the node's whole job
is deciding what a token means, and a mocked issuer would agree with whatever the node
believed. `tests/realm/CONTRACT.md` says what that realm stands in for.

Four of M2's exit criteria live in this file:

* a new asset registered without sharing settings is `hidden` and `org`;
* one without a licence cannot be published;
* a service appears as `dcat:DataService` with its OpenAPI reference;
* a `sensitive` dataset is refused on a BVR-operated node.
"""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from starlette.testclient import TestClient

from circuless_node import dcat
from circuless_node.app import create_public_app
from circuless_node.models import CataloguePush, Resource, Tenant
from circuless_node.settings import NodeOperator, Settings
from circuless_node.tenancy import tenant_scope
from circuless_node.vocabularies import Discoverability, Visibility

from .harness.keycloak import FixtureRealm

ALPHA_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
BETA_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000b1")


@pytest.fixture
def node(realm: FixtureRealm, tmp_path) -> Settings:
    """A BVR-operated node — the default, and the one D22 restricts."""
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
    SQLModel.metadata.create_all(built.state.engine)
    with Session(built.state.engine) as session:
        session.add(Tenant(org_id=ALPHA_ORG, group_path="/orgs/alpha", slug="alpha"))
        session.add(Tenant(org_id=BETA_ORG, group_path="/orgs/beta", slug="beta"))
        session.commit()
    return built


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(realm: FixtureRealm) -> dict[str, str]:
    return bearer(realm.user_token("alpha.admin"))


DATASET = {
    "slug": "batch-7",
    "kind": "dataset",
    "title": "Recycled PET batch 7",
    "theme": "material-characterisation",
    "classification": "non-sensitive",
}

SERVICE = {
    "slug": "optimiser",
    "kind": "service",
    "title": "Process optimiser",
    "theme": "processing",
    "classification": "non-sensitive",
    "endpoint_url": "http://optimiser.internal:8080",
    "openapi_ref": "https://alpha.example/optimiser/openapi.json",
}


# --- defaults (NFR4) ------------------------------------------------------------------


def test_a_new_resource_is_hidden_and_org(client: TestClient, admin: dict[str, str]) -> None:
    """M2 exit criterion. Registering publishes nothing; advertising takes a second call."""
    body = client.post("/v1/t/alpha/resources", headers=admin, json=DATASET).json()
    assert body["discoverability"] == Discoverability.HIDDEN.value
    assert body["visibility"] == Visibility.ORG.value
    assert body["status"] == "active"


# --- licence (NFR9) -------------------------------------------------------------------


def test_a_resource_without_a_licence_cannot_be_published(
    client: TestClient, admin: dict[str, str]
) -> None:
    """M2 exit criterion. Null is fine while hidden — registering before the terms are
    settled is reasonable — and refused the moment it would be advertised."""
    response = client.post(
        "/v1/t/alpha/resources",
        headers=admin,
        json={**DATASET, "discoverability": "catalogue"},
    )
    assert response.status_code == 422
    assert response.json()["reason"] == "licence_required"


def test_publishing_by_patch_also_needs_a_licence(
    client: TestClient, admin: dict[str, str]
) -> None:
    """The rule is about the resulting state, not about what was sent.

    Raising discoverability alone has to satisfy the licence rule using the licence
    already there — which is how a two-step publish would otherwise slip past a check
    written only for registration.
    """
    created = client.post("/v1/t/alpha/resources", headers=admin, json=DATASET).json()
    response = client.patch(
        f"/v1/t/alpha/resources/{created['id']}",
        headers=admin,
        json={"discoverability": "catalogue"},
    )
    assert response.status_code == 422
    assert response.json()["reason"] == "licence_required"


def test_clearing_the_licence_of_a_published_resource_is_refused(
    client: TestClient, admin: dict[str, str]
) -> None:
    """The other direction of the same rule, and the one easier to forget."""
    created = client.post(
        "/v1/t/alpha/resources",
        headers=admin,
        json={**DATASET, "licence": "CC-BY-4.0", "discoverability": "catalogue"},
    ).json()
    response = client.patch(
        f"/v1/t/alpha/resources/{created['id']}", headers=admin, json={"licence": None}
    )
    assert response.status_code == 422


def test_an_unknown_licence_is_refused(client: TestClient, admin: dict[str, str]) -> None:
    """Controlled list, taken literally. An unresolvable licence is worse than none,
    because it looks like one."""
    response = client.post(
        "/v1/t/alpha/resources",
        headers=admin,
        json={**DATASET, "licence": "WTFPL"},
    )
    assert response.status_code == 422
    assert response.json()["reason"] == "invalid_request"


def test_a_licensed_resource_can_be_published(client: TestClient, admin: dict[str, str]) -> None:
    body = client.post(
        "/v1/t/alpha/resources",
        headers=admin,
        json={**DATASET, "licence": "CC-BY-4.0", "discoverability": "catalogue"},
    ).json()
    assert body["discoverability"] == "catalogue"
    # Still closed for reading. The two settings are independent, and this is the normal
    # case: discoverable to everyone, readable only under an agreement.
    assert body["visibility"] == "org"


# --- classification (D22, H3) -----------------------------------------------------------


def test_a_bvr_operated_node_refuses_sensitive(client: TestClient, admin: dict[str, str]) -> None:
    """M2 exit criterion, and the default.

    Not because this node is less secure, but because BVR holding a partner's sensitive
    data on their behalf is the arrangement the project undertook not to make.
    """
    response = client.post(
        "/v1/t/alpha/resources",
        headers=admin,
        json={**DATASET, "classification": "sensitive"},
    )
    assert response.status_code == 422
    assert response.json()["reason"] == "classification_not_permitted"


def test_a_partner_node_accepts_sensitive(
    realm: FixtureRealm, admin: dict[str, str], tmp_path
) -> None:
    partner = Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        issuer=realm.issuer,
        operator=NodeOperator.PARTNER,
        database_url=f"sqlite:///{tmp_path / 'partner.db'}",
        data_dir=tmp_path / "partner-data",
    )
    app = create_public_app(partner)
    SQLModel.metadata.create_all(app.state.engine)
    with Session(app.state.engine) as session:
        session.add(Tenant(org_id=ALPHA_ORG, group_path="/orgs/alpha", slug="alpha"))
        session.commit()

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/v1/t/alpha/resources", headers=admin, json={**DATASET, "classification": "sensitive"}
    )
    assert response.status_code == 201


def test_a_node_nobody_configured_refuses_sensitive() -> None:
    """Fail closed. The permissive value is the one somebody has to type."""
    assert Settings(node_id="unconfigured").refuses_sensitive is True  # type: ignore[call-arg]


def test_patching_to_sensitive_is_refused_too(client: TestClient, admin: dict[str, str]) -> None:
    created = client.post("/v1/t/alpha/resources", headers=admin, json=DATASET).json()
    response = client.patch(
        f"/v1/t/alpha/resources/{created['id']}",
        headers=admin,
        json={"classification": "sensitive"},
    )
    assert response.status_code == 422


# --- datasets and services are different things (R15) -------------------------------------


def test_a_service_is_a_dcat_dataservice_with_its_openapi_reference(
    client: TestClient, admin: dict[str, str], node: Settings
) -> None:
    """M2 exit criterion, and review finding R15 — a service is not a Dataset with a URL."""
    created = client.post("/v1/t/alpha/resources", headers=admin, json=SERVICE).json()

    with (
        Session(create_engine(node.database_url)) as session,
        tenant_scope(session, _tenant_id(session, "alpha")),
    ):
        resource = session.get(Resource, uuid.UUID(created["id"]))
        record = dcat.render(resource, node_id=node.node_id, tenant_slug="alpha")

    assert record["@type"] == "dcat:DataService"
    assert record["dcat:endpointDescription"] == SERVICE["openapi_ref"]
    assert "dcat:distribution" not in record


def test_a_dataset_is_a_dcat_dataset_with_a_distribution(
    client: TestClient, admin: dict[str, str], node: Settings
) -> None:
    created = client.post(
        "/v1/t/alpha/resources", headers=admin, json={**DATASET, "licence": "CC-BY-4.0"}
    ).json()

    with (
        Session(create_engine(node.database_url)) as session,
        tenant_scope(session, _tenant_id(session, "alpha")),
    ):
        resource = session.get(Resource, uuid.UUID(created["id"]))
        record = dcat.render(resource, node_id=node.node_id, tenant_slug="alpha")

    assert record["@type"] == "dcat:Dataset"
    assert record["dcat:distribution"][0]["dcat:accessURL"].endswith("/data")
    assert record["dct:license"].startswith("https://spdx.org/")


def test_the_catalogue_record_never_carries_the_upstream_endpoint(
    client: TestClient, admin: dict[str, str], node: Settings
) -> None:
    """A consumer that learned the real address could go round the node — past decide(),
    past the agreement check, past the log. `endpointURL` is the node's own /invoke."""
    created = client.post("/v1/t/alpha/resources", headers=admin, json=SERVICE).json()

    with (
        Session(create_engine(node.database_url)) as session,
        tenant_scope(session, _tenant_id(session, "alpha")),
    ):
        resource = session.get(Resource, uuid.UUID(created["id"]))
        record = dcat.render(resource, node_id=node.node_id, tenant_slug="alpha")

    assert SERVICE["endpoint_url"] not in str(record)
    assert record["dcat:endpointURL"].endswith("/invoke")


def test_the_catalogue_record_never_carries_the_visibility(
    client: TestClient, admin: dict[str, str], node: Settings
) -> None:
    """The catalogue is filtered by discoverability, never by visibility. Publishing the
    access policy of every resource to everyone who can search is the opposite."""
    created = client.post(
        "/v1/t/alpha/resources",
        headers=admin,
        json={**DATASET, "licence": "CC-BY-4.0", "visibility": "agreement"},
    ).json()

    with (
        Session(create_engine(node.database_url)) as session,
        tenant_scope(session, _tenant_id(session, "alpha")),
    ):
        resource = session.get(Resource, uuid.UUID(created["id"]))
        record = dcat.render(resource, node_id=node.node_id, tenant_slug="alpha")

    assert "visibility" not in str(record)


def test_a_dataset_may_not_carry_service_fields(client: TestClient, admin: dict[str, str]) -> None:
    response = client.post(
        "/v1/t/alpha/resources",
        headers=admin,
        json={**DATASET, "endpoint_url": "http://x.invalid"},
    )
    assert response.status_code == 422


def test_a_service_needs_an_endpoint(client: TestClient, admin: dict[str, str]) -> None:
    payload = {k: v for k, v in SERVICE.items() if k != "endpoint_url"}
    assert client.post("/v1/t/alpha/resources", headers=admin, json=payload).status_code == 422


# --- management authorisation (N18) -----------------------------------------------------


def test_a_plain_member_cannot_register(client: TestClient, realm: FixtureRealm) -> None:
    response = client.post(
        "/v1/t/alpha/resources", headers=bearer(realm.user_token("alpha.user")), json=DATASET
    )
    assert response.status_code == 403


def test_another_organisations_admin_cannot_register(
    client: TestClient, realm: FixtureRealm
) -> None:
    """Scoping is not authorisation. Without N18 this caller would get a correctly
    scoped, entirely unauthorised write."""
    response = client.post(
        "/v1/t/alpha/resources", headers=bearer(realm.user_token("admins.only")), json=DATASET
    )
    assert response.status_code == 403


def test_a_node_token_cannot_register(client: TestClient, realm: FixtureRealm) -> None:
    """D14 — refused at N2 before this code runs, asserted end to end anyway."""
    response = client.post(
        "/v1/t/alpha/resources",
        headers=bearer(realm.node_token("node:test-node")),
        json=DATASET,
    )
    assert response.status_code == 403
    assert response.json()["reason"] == "node_principal_not_permitted"


def test_an_unknown_tenant_is_404_not_403(client: TestClient, admin: dict[str, str]) -> None:
    """Whether an organisation exists elsewhere in CIRCULess is not this node's to
    disclose, so a tenant it does not host is simply not found."""
    response = client.post("/v1/t/gamma/resources", headers=admin, json=DATASET)
    assert response.status_code == 404


# --- tenancy (N4) -------------------------------------------------------------------------


def test_one_tenants_resource_is_invisible_to_another(
    client: TestClient, realm: FixtureRealm, admin: dict[str, str]
) -> None:
    created = client.post("/v1/t/alpha/resources", headers=admin, json=DATASET).json()
    beta_admin = bearer(realm.user_token("admins.only"))

    listed = client.get("/v1/t/beta/resources", headers=beta_admin)
    assert listed.json() == []

    # And not by id either: the filter is on the query, not on the listing.
    direct = client.get(f"/v1/t/beta/resources/{created['id']}", headers=beta_admin)
    assert direct.status_code == 404


def test_the_same_slug_is_free_in_another_tenant(
    client: TestClient, realm: FixtureRealm, admin: dict[str, str]
) -> None:
    """Two organisations naming something `batch-7` is not a conflict, and making it one
    would disclose that the other name exists."""
    assert client.post("/v1/t/alpha/resources", headers=admin, json=DATASET).status_code == 201
    other = client.post(
        "/v1/t/beta/resources", headers=bearer(realm.user_token("admins.only")), json=DATASET
    )
    assert other.status_code == 201


def test_a_duplicate_slug_in_the_same_tenant_is_refused(
    client: TestClient, admin: dict[str, str]
) -> None:
    client.post("/v1/t/alpha/resources", headers=admin, json=DATASET)
    response = client.post("/v1/t/alpha/resources", headers=admin, json=DATASET)
    assert response.status_code == 409


# --- the catalogue push flag (N5 -> N7) ---------------------------------------------------


def test_registering_marks_the_tenant_for_a_push(
    client: TestClient, admin: dict[str, str], node: Settings
) -> None:
    """Marked, not pushed. Registration must not fail because the Cloud is unreachable —
    a control-plane outage becoming a data-plane one is what F16 and D1 prevent."""
    client.post("/v1/t/alpha/resources", headers=admin, json=DATASET)

    with Session(create_engine(node.database_url)) as session:
        pending = session.exec(select(CataloguePush)).all()
    assert len(pending) == 1


def test_several_changes_are_one_pending_push(
    client: TestClient, admin: dict[str, str], node: Settings
) -> None:
    """What N7 sends is the tenant's whole catalogue, so ten edits before the next push
    are one push."""
    created = client.post("/v1/t/alpha/resources", headers=admin, json=DATASET).json()
    client.patch(f"/v1/t/alpha/resources/{created['id']}", headers=admin, json={"title": "b"})
    client.patch(f"/v1/t/alpha/resources/{created['id']}", headers=admin, json={"title": "c"})

    with Session(create_engine(node.database_url)) as session:
        assert len(session.exec(select(CataloguePush)).all()) == 1


def test_the_push_flag_is_node_global_and_not_tenant_filtered(
    client: TestClient, admin: dict[str, str], node: Settings
) -> None:
    """R10. Reading it without a tenant scope must work — N7 does exactly that, and a
    filter here would break the push it exists to drive."""
    client.post("/v1/t/alpha/resources", headers=admin, json=DATASET)
    with Session(create_engine(node.database_url)) as session:
        # No tenant_scope, deliberately. A TenantOwned table would raise here.
        assert session.exec(select(CataloguePush)).all()


def _tenant_id(session: Session, slug: str) -> uuid.UUID:
    return session.exec(select(Tenant).where(Tenant.slug == slug)).one().id
