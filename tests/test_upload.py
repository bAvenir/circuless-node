"""Putting a dataset's bytes on the node (N19).

The mirror of `test_transfer.py`, and the differences are the point: uploading is
authorised by N18's management table rather than by `decide()`, so an agreement never
grants it and visibility never affects it.

Two properties get most of the attention here, because both are the kind that look
fine until the day they are needed:

* **the size limit is enforced on the stream**, not only on `Content-Length`, since a
  chunked request has no `Content-Length` to check;
* **a failed upload leaves the previous bytes untouched**, because the write goes to a
  temporary name and is renamed only once it has fully arrived.
"""

from __future__ import annotations

import uuid

import pytest
from sqlmodel import Session, col, select
from starlette.testclient import TestClient

from circuless_node.app import create_public_app
from circuless_node.models import AccessLog, CataloguePush, Resource, Tenant
from circuless_node.settings import Settings
from circuless_node.storage import STAGING_DIR, resource_location, staging_location
from circuless_node.tenancy import all_tenants, tenant_scope
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

PAYLOAD = b"spectrum,intensity\n400,0.21\n"


@pytest.fixture
def node(realm: FixtureRealm, tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        issuer=realm.issuer,
        database_url=f"sqlite:///{tmp_path / 'node.db'}",
        data_dir=tmp_path / "data",
        cors_allow_origins=["https://ui.circuless.eu"],
        # Small enough to exceed in a test without writing a gigabyte.
        max_upload_bytes=1024,
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


def make_resource(
    app,
    *,
    slug: str = "batch-7",
    tenant: str = "alpha",
    kind: ResourceKind = ResourceKind.DATASET,
    shape: Shape = Shape.FILE,
    status: ResourceStatus = ResourceStatus.ACTIVE,
    storage_path: str | None = None,
    endpoint_url: str | None = None,
) -> uuid.UUID:
    tid = tenant_id(app, tenant)
    with Session(app.state.engine) as session, tenant_scope(session, tid):
        resource = Resource(
            tenant_id=tid,
            slug=slug,
            kind=kind,
            shape=shape,
            title="Recycled PET batch 7",
            theme=Theme.MATERIAL_CHARACTERISATION,
            classification=Classification.NON_SENSITIVE,
            discoverability=Discoverability.HIDDEN,
            visibility=Visibility.ORG,
            status=status,
            storage_path=storage_path,
            endpoint_url=endpoint_url,
        )
        session.add(resource)
        session.commit()
        session.refresh(resource)
        return resource.id


def url(resource_id: uuid.UUID, tenant: str = "alpha", suffix: str = "") -> str:
    return f"/v1/t/{tenant}/resources/{resource_id}/data{suffix}"


def stored(app, resource_id: uuid.UUID, name: str, tenant: str = "alpha") -> bytes:
    with app.state.storage.open(
        tenant_id(app, tenant), resource_location(resource_id, name), "rb"
    ) as handle:
        return handle.read()


def log_rows(app) -> list[AccessLog]:
    with Session(app.state.engine) as session, all_tenants(session):
        return list(session.exec(select(AccessLog).order_by(col(AccessLog.ts))).all())


# --- the happy path, and the round trip -----------------------------------------------


def test_an_admin_uploads_and_can_read_it_back(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app)
    response = client.put(url(resource_id), headers=alpha_admin, content=PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {"bytes": len(PAYLOAD), "path": "batch-7"}
    assert client.get(url(resource_id), headers=alpha_admin).content == PAYLOAD


def test_the_name_inside_the_directory_comes_from_storage_path(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """The provider still names the file; the node still decides where it lands."""
    resource_id = make_resource(app, storage_path="batch-7.csv")
    client.put(url(resource_id), headers=alpha_admin, content=PAYLOAD)

    assert stored(app, resource_id, "batch-7.csv") == PAYLOAD
    download = client.get(url(resource_id), headers=alpha_admin)
    assert "batch-7.csv" in download.headers["content-disposition"]


def test_a_service_principal_may_upload(client: TestClient, app, realm: FixtureRealm) -> None:
    """N18: publishing yesterday's run is what a pipeline account is for."""
    resource_id = make_resource(app)
    token = bearer(realm.service_token())
    assert client.put(url(resource_id), headers=token, content=PAYLOAD).status_code == 200


def test_the_upload_is_logged_with_the_bytes_received(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app)
    client.put(url(resource_id), headers=alpha_admin, content=PAYLOAD)

    entry = log_rows(app)[-1]
    assert entry.action == "resource_upload"
    assert entry.decision == "allow"
    assert entry.resource_id == resource_id
    assert entry.bytes == len(PAYLOAD)


def test_an_upload_marks_the_catalogue_dirty(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """Size and modification date changed, so the DCAT record is stale. Marked, not
    pushed: pushing here would make an upload fail whenever the Cloud is down (F16)."""
    resource_id = make_resource(app)
    tid = tenant_id(app)
    with Session(app.state.engine) as session:
        for pending in session.exec(select(CataloguePush)).all():
            session.delete(pending)
        session.commit()

    client.put(url(resource_id), headers=alpha_admin, content=PAYLOAD)

    with Session(app.state.engine) as session:
        assert session.get(CataloguePush, tid) is not None


# --- who may not ------------------------------------------------------------------------


def test_a_member_may_not_upload(client: TestClient, app, alpha_user: dict[str, str]) -> None:
    """Membership lets you consume under `visibility=org`; publishing on the
    organisation's behalf needs an admin or a service principal."""
    resource_id = make_resource(app)
    response = client.put(url(resource_id), headers=alpha_user, content=PAYLOAD)

    assert response.status_code == 403
    assert log_rows(app)[-1].decision == "deny"


def test_another_org_may_not_upload(client: TestClient, app, beta_admin: dict[str, str]) -> None:
    resource_id = make_resource(app)
    assert client.put(url(resource_id), headers=beta_admin, content=PAYLOAD).status_code == 403


def test_a_node_principal_may_not_upload(client: TestClient, app, realm: FixtureRealm) -> None:
    """D14, on a management endpoint as well as a consumption one."""
    resource_id = make_resource(app)
    response = client.put(url(resource_id), headers=bearer(realm.node_token()), content=PAYLOAD)
    assert response.status_code == 403
    assert response.json()["reason"] == "node_principal_not_permitted"


def test_an_agreement_does_not_grant_write(
    client: TestClient, app, beta_admin: dict[str, str]
) -> None:
    """There is no visibility and no agreement under which an outsider may upload —
    writing is N18's table, which an agreement never enters."""
    resource_id = make_resource(app)
    assert client.put(url(resource_id), headers=beta_admin, content=PAYLOAD).status_code == 403


def test_uploading_to_a_withdrawn_resource_is_refused(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """D25. It would quietly resurrect data a purge is already scheduled to remove."""
    resource_id = make_resource(app, status=ResourceStatus.WITHDRAWN)
    response = client.put(url(resource_id), headers=alpha_admin, content=PAYLOAD)
    assert response.status_code == 404


def test_a_service_resource_has_no_stored_data(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(
        app,
        kind=ResourceKind.SERVICE,
        shape=Shape.SERVICE,
        endpoint_url="http://optimiser.internal:8080",
    )
    response = client.put(url(resource_id), headers=alpha_admin, content=PAYLOAD)
    assert response.status_code == 400
    assert response.json()["reason"] == "unsupported"


# --- the size limit, enforced twice -------------------------------------------------------


def test_content_length_over_the_limit_is_refused_before_reading(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app)
    response = client.put(url(resource_id), headers=alpha_admin, content=b"x" * 2048)

    assert response.status_code == 413
    assert response.json()["reason"] == "payload_too_large"


def test_a_chunked_upload_over_the_limit_is_stopped_mid_stream(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """The case `Content-Length` cannot cover: a chunked request does not declare a
    size, so a limit that only reads the header is a limit anyone can skip by not
    sending one."""

    def chunks():
        for _ in range(4):
            yield b"x" * 512

    response = client.put(url(make_resource(app)), headers=alpha_admin, content=chunks())

    assert response.status_code == 413
    assert response.json()["reason"] == "payload_too_large"


def test_a_malformed_content_length_is_a_bad_request(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app)
    response = client.put(
        url(resource_id),
        headers={**alpha_admin, "Content-Length": "not-a-number", "Transfer-Encoding": ""},
        content=b"x",
    )
    assert response.status_code in (400, 413, 422)


# --- nothing is overwritten until it has fully arrived ---------------------------------


def test_an_oversized_upload_leaves_the_previous_bytes_intact(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """The reason uploads stage and rename. Without it, a refused upload would have
    already truncated the file — and N8 streams straight off disk, so a reader would
    have served the wreckage."""
    resource_id = make_resource(app)
    assert client.put(url(resource_id), headers=alpha_admin, content=PAYLOAD).status_code == 200

    def chunks():
        for _ in range(4):
            yield b"y" * 512

    assert client.put(url(resource_id), headers=alpha_admin, content=chunks()).status_code == 413

    assert stored(app, resource_id, "batch-7") == PAYLOAD
    assert client.get(url(resource_id), headers=alpha_admin).content == PAYLOAD


def test_a_failed_upload_leaves_no_staging_file_behind(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app)

    def chunks():
        for _ in range(4):
            yield b"y" * 512

    client.put(url(resource_id), headers=alpha_admin, content=chunks())

    objects, _ = app.state.storage.list_objects(tenant_id(app), resource_location(resource_id), 100)
    assert [item.path for item in objects] == []


def test_a_leftover_staging_file_is_invisible_to_readers(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """Staging lives outside the resource's directory, so a part-written upload — or
    one left behind by a crash before cleanup — can never be listed in a manifest or
    fetched through `/data/{path}`. Written here directly rather than by racing an
    upload, because the property is about the layout, not about timing.
    """
    resource_id = make_resource(app, shape=Shape.BUCKET)
    tid = tenant_id(app)
    with app.state.storage.open(tid, staging_location(f"{resource_id}-half"), "wb") as handle:
        handle.write(b"half written")

    manifest = client.get(url(resource_id), headers=alpha_admin).json()
    assert manifest["objects"] == []

    escape = client.get(
        url(resource_id, suffix=f"/..%2f{STAGING_DIR}%2f{resource_id}-half"),
        headers=alpha_admin,
    )
    assert escape.status_code == 400
    assert escape.json()["reason"] == "path_not_allowed"


def test_an_upload_replaces_the_previous_bytes(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app)
    client.put(url(resource_id), headers=alpha_admin, content=PAYLOAD)
    client.put(url(resource_id), headers=alpha_admin, content=b"replaced")

    assert stored(app, resource_id, "batch-7") == b"replaced"


# --- buckets -----------------------------------------------------------------------------


def test_bucket_objects_upload_individually_and_appear_in_the_manifest(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app, shape=Shape.BUCKET)
    assert (
        client.put(
            url(resource_id, suffix="/spectra/run-07.csv"), headers=alpha_admin, content=PAYLOAD
        ).status_code
        == 200
    )

    manifest = client.get(url(resource_id), headers=alpha_admin).json()
    assert [item["path"] for item in manifest["objects"]] == ["spectra/run-07.csv"]
    assert (
        client.get(url(resource_id, suffix="/spectra/run-07.csv"), headers=alpha_admin).content
        == PAYLOAD
    )


def test_uploading_to_a_bucket_without_a_path_is_a_shape_error(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app, shape=Shape.BUCKET)
    response = client.put(url(resource_id), headers=alpha_admin, content=PAYLOAD)
    assert response.status_code == 400
    assert response.json()["reason"] == "unsupported"


def test_uploading_a_path_to_a_file_resource_is_a_shape_error(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app, shape=Shape.FILE)
    response = client.put(
        url(resource_id, suffix="/extra.csv"), headers=alpha_admin, content=PAYLOAD
    )
    assert response.status_code == 400
    assert response.json()["reason"] == "unsupported"


# --- path confinement (H3) -----------------------------------------------------------------


@pytest.mark.parametrize(
    "traversal",
    ["%2e%2e%2f%2e%2e%2fnode.db", "%2fetc%2fpasswd", "https:%2f%2fevil.example%2fx"],
)
def test_an_upload_cannot_escape_its_resource_directory(
    client: TestClient, app, alpha_admin: dict[str, str], traversal: str
) -> None:
    resource_id = make_resource(app, shape=Shape.BUCKET)
    response = client.put(
        url(resource_id, suffix=f"/{traversal}"), headers=alpha_admin, content=PAYLOAD
    )
    assert response.status_code == 400, response.text
    assert response.json()["reason"] == "path_not_allowed"


def test_a_storage_path_that_escapes_is_refused_at_registration(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """Told at the moment it is typed, rather than at the first upload."""
    response = client.post(
        "/v1/t/alpha/resources",
        headers=alpha_admin,
        json={
            "slug": "escape",
            "kind": "dataset",
            "title": "Escape",
            "theme": "processing",
            "classification": "non-sensitive",
            "storage_path": "../../node.db",
        },
    )
    assert response.status_code == 400
    assert response.json()["reason"] == "path_not_allowed"


# --- one resource can never name another's bytes -------------------------------------------


def test_two_resources_cannot_share_a_file(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """The hazard the node-owned layout exists to remove.

    Before N19 the provider chose the location outright, so a second resource could name
    the first one's file — and since N18 lets a service principal register resources, a
    compromised pipeline account could publish anything the organisation held by
    pointing a new `visibility=agreement` resource at a private one's path, without
    uploading a byte.

    Now the directory is derived from the resource id, so the same `storage_path` on two
    resources names two different files.
    """
    private = make_resource(app, slug="private-one", storage_path="shared.csv")
    shared = make_resource(app, slug="shared-one", storage_path="shared.csv")

    client.put(url(private), headers=alpha_admin, content=b"secret")
    client.put(url(shared), headers=alpha_admin, content=b"public")

    assert stored(app, private, "shared.csv") == b"secret"
    assert stored(app, shared, "shared.csv") == b"public"
    assert client.get(url(private), headers=alpha_admin).content == b"secret"
    assert client.get(url(shared), headers=alpha_admin).content == b"public"
