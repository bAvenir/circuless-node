"""Upstream service credentials (N10, invariant 11).

The node holds a partner's credential to a partner's system. Three properties carry the
whole component, and each is tested as a property rather than as a code path:

* **it never comes back out** — not from any endpoint, not in any error, not in the log;
* **a service principal can never set it**, though it may do everything else N18 allows;
* **it is ciphertext at rest**, and the key is a `0600` file the node refuses to use if
  that has loosened.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import uuid
from pathlib import Path

import pytest
from sqlmodel import Session, col, select
from starlette.testclient import TestClient

from circuless_node.app import create_public_app
from circuless_node.credentials import (
    fernet_key_path,
    load_or_create_fernet,
    render,
    unseal,
)
from circuless_node.identity import KeyPermissionsError
from circuless_node.models import AccessLog, Resource, ServiceCredential, Tenant
from circuless_node.purge import purge_due
from circuless_node.settings import Settings
from circuless_node.tenancy import TenancyNotScopedError, all_tenants, tenant_scope
from circuless_node.vocabularies import (
    Classification,
    Discoverability,
    ResourceKind,
    ResourceStatus,
    Shape,
    Theme,
    Visibility,
)

from .harness.keycloak import FixtureRealm
from .harness.migrated import migrated_engine

ALPHA_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
BETA_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

SECRET = "sk-live-do-not-leak-7f3a91"


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
    built.state.engine = migrated_engine(node)
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
def alpha_admin(realm: FixtureRealm) -> dict[str, str]:
    return bearer(realm.user_token("alpha.admin"))


@pytest.fixture
def alpha_user(realm: FixtureRealm) -> dict[str, str]:
    return bearer(realm.user_token("alpha.user"))


@pytest.fixture
def beta_admin(realm: FixtureRealm) -> dict[str, str]:
    return bearer(realm.user_token("admins.only"))


def tenant_id(app, slug: str = "alpha") -> uuid.UUID:
    with Session(app.state.engine) as session:
        return session.exec(select(Tenant).where(Tenant.slug == slug)).one().id


def make_service(
    app,
    *,
    slug: str = "optimiser",
    kind: ResourceKind = ResourceKind.SERVICE,
    status: ResourceStatus = ResourceStatus.ACTIVE,
) -> uuid.UUID:
    tid = tenant_id(app)
    with Session(app.state.engine) as session, tenant_scope(session, tid):
        resource = Resource(
            tenant_id=tid,
            slug=slug,
            kind=kind,
            shape=Shape.SERVICE if kind is ResourceKind.SERVICE else Shape.FILE,
            title="Process optimiser",
            theme=Theme.PROCESSING,
            classification=Classification.NON_SENSITIVE,
            discoverability=Discoverability.HIDDEN,
            visibility=Visibility.ORG,
            status=status,
            endpoint_url="http://optimiser.internal:8080" if kind is ResourceKind.SERVICE else None,
        )
        session.add(resource)
        session.commit()
        session.refresh(resource)
        return resource.id


def url(resource_id: uuid.UUID, tenant: str = "alpha") -> str:
    return f"/v1/t/{tenant}/resources/{resource_id}/credential"


def stored(app, resource_id: uuid.UUID) -> ServiceCredential | None:
    with Session(app.state.engine) as session, all_tenants(session):
        return session.get(ServiceCredential, resource_id)


BEARER_BODY = {"scheme": "bearer", "secret": SECRET}


# --- it never comes back out ----------------------------------------------------------


def test_the_secret_is_never_returned_by_any_endpoint(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """The property, swept across every response the credential routes can produce,
    rather than one assertion per handler — a fourth endpoint added later is covered by
    the sweep and not by a test someone remembered to extend."""
    resource_id = make_service(app)

    responses = [
        client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY),
        client.get(url(resource_id), headers=alpha_admin),
        client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY),  # rotate
        client.get(f"/v1/t/alpha/resources/{resource_id}", headers=alpha_admin),
        client.get("/v1/t/alpha/resources", headers=alpha_admin),
        client.delete(url(resource_id), headers=alpha_admin),
        client.get(url(resource_id), headers=alpha_admin),  # 404 now
    ]
    for response in responses:
        assert SECRET not in response.text, f"{response.request.url} leaked the secret"


def test_the_secret_is_never_in_the_access_log(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_service(app)
    client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY)

    with Session(app.state.engine) as session, all_tenants(session):
        entries = session.exec(select(AccessLog)).all()
        dumped = json.dumps([entry.model_dump(mode="json") for entry in entries])
    assert SECRET not in dumped
    assert any(entry.action == "credential_set" for entry in entries)


def test_metadata_says_everything_except_the_value(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_service(app)
    client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY)

    body = client.get(url(resource_id), headers=alpha_admin).json()
    assert body["scheme"] == "bearer"
    assert body["header_name"] is None
    assert uuid.UUID(body["set_by"])
    assert set(body) == {"scheme", "header_name", "set_by", "created_at", "updated_at"}


# --- at rest --------------------------------------------------------------------------


def test_it_is_ciphertext_in_the_database(
    client: TestClient, app, alpha_admin: dict[str, str], node: Settings
) -> None:
    resource_id = make_service(app)
    client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY)

    credential = stored(app, resource_id)
    assert SECRET.encode() not in credential.secret
    assert unseal(load_or_create_fernet(node), credential.secret) == SECRET


def test_the_whole_database_file_contains_no_plaintext(
    client: TestClient, app, alpha_admin: dict[str, str], node: Settings
) -> None:
    """Past the ORM and at the bytes on disk, because "it is encrypted" is a claim
    about the file an operator might copy, not about a column."""
    resource_id = make_service(app)
    client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY)

    database = node.database_url.split("///", 1)[1]
    assert SECRET.encode() not in Path(database).read_bytes()


def test_the_key_file_is_owner_only(node: Settings) -> None:
    load_or_create_fernet(node)
    mode = fernet_key_path(node).stat().st_mode & 0o777
    assert mode == 0o600, f"the credential key has mode {mode:04o}"


def test_a_loosened_key_file_refuses_to_load(node: Settings) -> None:
    """Fixing it silently would hide that something loosened it — and every credential
    it protects should now be considered disclosed, which is a thing to say out loud."""
    load_or_create_fernet(node)
    fernet_key_path(node).chmod(0o644)

    with pytest.raises(KeyPermissionsError, match="exposed"):
        load_or_create_fernet(node)


def test_the_key_survives_a_restart(node: Settings) -> None:
    first = load_or_create_fernet(node)
    sealed = first.encrypt(b"x")
    assert load_or_create_fernet(node).decrypt(sealed) == b"x"


# --- who may (N18) ----------------------------------------------------------------------


def test_a_service_principal_may_not_set_a_credential(
    client: TestClient, app, realm: FixtureRealm
) -> None:
    """The one thing N18 refuses a service principal that it otherwise allows. A
    pipeline that could rotate this could point the node at a server of its choosing —
    and send it this credential on the way."""
    resource_id = make_service(app)
    token = bearer(realm.service_token())

    assert client.put(url(resource_id), headers=token, json=BEARER_BODY).status_code == 403
    assert stored(app, resource_id) is None


def test_a_member_may_not_set_a_credential(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    resource_id = make_service(app)
    assert client.put(url(resource_id), headers=alpha_user, json=BEARER_BODY).status_code == 403


def test_another_org_may_not_read_or_set(
    client: TestClient, app, alpha_admin: dict[str, str], beta_admin: dict[str, str]
) -> None:
    resource_id = make_service(app)
    client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY)

    assert client.get(url(resource_id), headers=beta_admin).status_code == 403
    assert client.put(url(resource_id), headers=beta_admin, json=BEARER_BODY).status_code == 403
    assert client.delete(url(resource_id), headers=beta_admin).status_code == 403


def test_a_node_principal_may_not(client: TestClient, app, realm: FixtureRealm) -> None:
    resource_id = make_service(app)
    response = client.put(url(resource_id), headers=bearer(realm.node_token()), json=BEARER_BODY)
    assert response.status_code == 403
    assert response.json()["reason"] == "node_principal_not_permitted"


# --- the schemes --------------------------------------------------------------------------


def test_bearer_renders_an_authorization_header(
    client: TestClient, app, alpha_admin: dict[str, str], node: Settings
) -> None:
    resource_id = make_service(app)
    client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY)

    credential = stored(app, resource_id)
    plaintext = unseal(load_or_create_fernet(node), credential.secret)
    assert render(credential, plaintext) == ("authorization", f"Bearer {SECRET}")


def test_an_api_key_renders_its_own_header(
    client: TestClient, app, alpha_admin: dict[str, str], node: Settings
) -> None:
    resource_id = make_service(app)
    client.put(
        url(resource_id),
        headers=alpha_admin,
        json={"scheme": "header", "secret": SECRET, "header_name": "X-API-Key"},
    )

    credential = stored(app, resource_id)
    plaintext = unseal(load_or_create_fernet(node), credential.secret)
    assert render(credential, plaintext) == ("x-api-key", SECRET)


def test_basic_keeps_the_username_inside_the_ciphertext(
    client: TestClient, app, alpha_admin: dict[str, str], node: Settings
) -> None:
    """Half a credential in plaintext is still half a credential."""
    resource_id = make_service(app)
    client.put(
        url(resource_id),
        headers=alpha_admin,
        json={"scheme": "basic", "username": "optimiser-bot", "secret": SECRET},
    )

    credential = stored(app, resource_id)
    assert b"optimiser-bot" not in credential.secret

    plaintext = unseal(load_or_create_fernet(node), credential.secret)
    name, value = render(credential, plaintext)
    assert name == "authorization"
    assert base64.b64decode(value.removeprefix("Basic ")).decode() == f"optimiser-bot:{SECRET}"


@pytest.mark.parametrize(
    "body",
    [
        {"scheme": "header", "secret": "x"},  # no header_name
        {"scheme": "bearer", "secret": "x", "header_name": "X-Api-Key"},
        {"scheme": "basic", "secret": "x"},  # no username
        {"scheme": "basic", "secret": "x", "username": "a:b"},
        {"scheme": "bearer", "secret": "x", "username": "a"},
        {"scheme": "bearer", "secret": ""},
    ],
)
def test_incoherent_credentials_are_refused(
    client: TestClient, app, alpha_admin: dict[str, str], body: dict
) -> None:
    resource_id = make_service(app)
    assert client.put(url(resource_id), headers=alpha_admin, json=body).status_code == 422


# --- the header allowlist (invariant 10) -----------------------------------------------------


@pytest.mark.parametrize(
    "header_name",
    [
        "Host",
        "Connection",
        "Transfer-Encoding",
        "Content-Length",
        "X-CIRCULess-Subject",
        "x-circuless-org",
        "Proxy-Authorization",
        "Bad Name",
        "X-Evil\r\nInjected",
        "with:colon",
    ],
)
def test_a_credential_may_not_set_a_reserved_or_malformed_header(
    client: TestClient, app, alpha_admin: dict[str, str], header_name: str
) -> None:
    """Invariant 10 says the node strips every inbound `X-CIRCULess-*` and injects its
    own. A credential allowed to name one could forge the identity the node vouches for,
    to the node's own upstream, with nothing in the request to show for it — and the
    token grammar is what makes CRLF injection impossible rather than unlikely."""
    resource_id = make_service(app)
    response = client.put(
        url(resource_id),
        headers=alpha_admin,
        json={"scheme": "header", "secret": SECRET, "header_name": header_name},
    )
    assert response.status_code == 422, response.text
    assert stored(app, resource_id) is None


# --- lifecycle --------------------------------------------------------------------------------


def test_only_a_service_resource_has_one(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    dataset = make_service(app, slug="batch-7", kind=ResourceKind.DATASET)
    response = client.put(url(dataset), headers=alpha_admin, json=BEARER_BODY)
    assert response.status_code == 400
    assert response.json()["reason"] == "unsupported"


def test_rotation_replaces_without_a_grace_period(
    client: TestClient, app, alpha_admin: dict[str, str], node: Settings
) -> None:
    """An upstream credential is rotated at the upstream; keeping the old one here
    would mean holding a secret its owner believes they have revoked."""
    resource_id = make_service(app)
    client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY)
    client.put(
        url(resource_id),
        headers=alpha_admin,
        json={"scheme": "bearer", "secret": "sk-live-the-new-one"},
    )

    credential = stored(app, resource_id)
    assert unseal(load_or_create_fernet(node), credential.secret) == "sk-live-the-new-one"
    assert credential.updated_at >= credential.created_at


def test_withdrawing_the_service_destroys_the_credential_at_once(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """Not at the purge thirty days later. The retention window is so a provider can
    recover data deleted by mistake; a credential is not something they would want
    back, and holding a partner's secret after they said the service was gone is the
    wrong side of the trade."""
    resource_id = make_service(app)
    client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY)
    assert stored(app, resource_id) is not None

    client.delete(f"/v1/t/alpha/resources/{resource_id}", headers=alpha_admin)

    assert stored(app, resource_id) is None
    with Session(app.state.engine) as session, all_tenants(session):
        assert session.get(Resource, resource_id) is not None, "the resource itself survives"


def test_the_schema_will_not_let_a_credential_outlive_its_resource(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """ON DELETE CASCADE, proven by deleting the resource row directly — past the
    withdrawal path that normally clears it first. The purge has a list of things to
    remember and "the partner's password" is the worst possible item to have on it."""
    resource_id = make_service(app)
    client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY)

    with Session(app.state.engine) as session, all_tenants(session):
        session.delete(session.get(Resource, resource_id))
        session.commit()

    assert stored(app, resource_id) is None


def test_a_purge_takes_the_credential_with_it(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_service(app)
    client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY)
    client.delete(f"/v1/t/alpha/resources/{resource_id}", headers=alpha_admin)

    purge_due(
        app.state.engine, app.state.storage, now=dt.datetime.now(dt.UTC) + dt.timedelta(days=31)
    )

    assert stored(app, resource_id) is None
    with Session(app.state.engine) as session, all_tenants(session):
        assert session.get(Resource, resource_id) is None


def test_a_withdrawn_service_cannot_take_a_new_credential(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_service(app, status=ResourceStatus.WITHDRAWN)
    response = client.put(url(resource_id), headers=alpha_admin, json=BEARER_BODY)
    assert response.status_code == 409


def test_reading_a_credential_that_is_not_set(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_service(app)
    assert client.get(url(resource_id), headers=alpha_admin).status_code == 404
    assert client.delete(url(resource_id), headers=alpha_admin).status_code == 404


def test_tenancy_covers_the_credential_table(app) -> None:
    """`ServiceCredential` is tenant-owned by shape, so N4's filter applies without a
    handler writing one."""
    from circuless_node.models import TenantOwned

    assert issubclass(ServiceCredential, TenantOwned)
    with Session(app.state.engine) as session, pytest.raises(TenancyNotScopedError):
        session.exec(select(ServiceCredential).where(col(ServiceCredential.scheme) == "x"))
