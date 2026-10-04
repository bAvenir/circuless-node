"""Two-stage deletion (N20, D25, invariant 14).

Stage one marks the resource withdrawn and removes nothing. Stage two, after
`purge_after`, removes the bytes and the row — and nothing else.

The cases worth writing are the ones where the two stages differ: what a consumer can
reach between them (nothing), what the owner can see (that it is going, and when), and
what survives the purge (the access log, pointing at something gone).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, col, select
from starlette.testclient import TestClient

from circuless_node.app import create_public_app
from circuless_node.models import AccessLog, AgreementCache, CataloguePush, Resource, Tenant
from circuless_node.purge import purge_due
from circuless_node.settings import Settings
from circuless_node.storage import resource_location, staging_location
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
        purge_after_days=30,
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
def beta_user(realm: FixtureRealm) -> dict[str, str]:
    return bearer(realm.user_token("beta.user"))


def tenant_id(app, slug: str = "alpha") -> uuid.UUID:
    with Session(app.state.engine) as session:
        return session.exec(select(Tenant).where(Tenant.slug == slug)).one().id


def make_resource(
    app,
    *,
    slug: str = "batch-7",
    visibility: Visibility = Visibility.ORG,
    discoverability: Discoverability = Discoverability.HIDDEN,
    content: bytes | None = PAYLOAD,
) -> uuid.UUID:
    tid = tenant_id(app)
    with Session(app.state.engine) as session, tenant_scope(session, tid):
        resource = Resource(
            tenant_id=tid,
            slug=slug,
            kind=ResourceKind.DATASET,
            shape=Shape.FILE,
            title="Recycled PET batch 7",
            theme=Theme.MATERIAL_CHARACTERISATION,
            classification=Classification.NON_SENSITIVE,
            licence="CC-BY-4.0",
            discoverability=discoverability,
            visibility=visibility,
            storage_path="batch-7.csv",
        )
        session.add(resource)
        session.commit()
        session.refresh(resource)
        resource_id = resource.id

    if content is not None:
        with app.state.storage.open(
            tid, resource_location(resource_id, "batch-7.csv"), "wb"
        ) as handle:
            handle.write(content)
    return resource_id


def url(resource_id: uuid.UUID, suffix: str = "") -> str:
    return f"/v1/t/alpha/resources/{resource_id}{suffix}"


def row(app, resource_id: uuid.UUID) -> Resource | None:
    with Session(app.state.engine) as session, all_tenants(session):
        return session.get(Resource, resource_id)


def log_rows(app) -> list[AccessLog]:
    with Session(app.state.engine) as session, all_tenants(session):
        return list(session.exec(select(AccessLog).order_by(col(AccessLog.ts))).all())


def bytes_exist(app, resource_id: uuid.UUID) -> bool:
    return app.state.storage.exists(tenant_id(app), resource_location(resource_id, "batch-7.csv"))


# --- stage one: withdraw --------------------------------------------------------------


def test_delete_marks_withdrawn_and_schedules_a_purge(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app)
    body = client.delete(url(resource_id), headers=alpha_admin).json()

    assert body["status"] == "withdrawn"
    assert body["withdrawn_at"] is not None
    scheduled = datetime.fromisoformat(body["purge_after"])
    assert timedelta(days=29) < scheduled - datetime.now(UTC) <= timedelta(days=30)


def test_delete_removes_nothing_yet(client: TestClient, app, alpha_admin: dict[str, str]) -> None:
    """The whole point of two stages. Deletion that is instant and irreversible is
    deletion nobody dares use."""
    resource_id = make_resource(app)
    client.delete(url(resource_id), headers=alpha_admin)

    assert bytes_exist(app, resource_id)
    assert row(app, resource_id) is not None


def test_a_withdrawn_resource_is_unreadable_immediately(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    """`decide()` denies it from the moment of the DELETE — the retention window is
    about recovery, not about continued access."""
    resource_id = make_resource(app)
    assert client.get(url(resource_id, "/data"), headers=alpha_user).status_code == 200

    admin_headers = {"Authorization": alpha_user["Authorization"]}
    with Session(app.state.engine) as session, all_tenants(session):
        resource = session.get(Resource, resource_id)
        resource.status = ResourceStatus.WITHDRAWN
        session.add(resource)
        session.commit()

    response = client.get(url(resource_id, "/data"), headers=admin_headers)
    assert response.status_code == 404
    assert response.json()["reason"] == "not_found"


def test_an_agreement_does_not_survive_withdrawal(
    client: TestClient, app, alpha_admin: dict[str, str], beta_user: dict[str, str]
) -> None:
    """D25 is about everyone, and a consumer holding a live agreement is the case that
    would otherwise slip through."""
    resource_id = make_resource(app, visibility=Visibility.AGREEMENT)
    with Session(app.state.engine) as session:
        session.add(
            AgreementCache(
                id=uuid.uuid4(),
                provider_org="alpha",
                consumer_org="beta",
                resource_id=resource_id,
                actions="read",
                valid_from=datetime.now(UTC) - timedelta(days=1),
                status="accepted",
            )
        )
        session.commit()

    assert client.get(url(resource_id, "/data"), headers=beta_user).status_code == 200
    client.delete(url(resource_id), headers=alpha_admin)
    assert client.get(url(resource_id, "/data"), headers=beta_user).status_code == 404


def test_withdrawal_drops_the_record_from_the_catalogue_push(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """Propagated by absence: the Cloud tombstones what a push no longer contains, so
    withdrawal needs no second call that could fail on its own."""
    from circuless_node.sync import catalogue_for

    resource_id = make_resource(app, discoverability=Discoverability.CATALOGUE)
    tid = tenant_id(app)

    def published() -> list:
        with Session(app.state.engine) as session:
            tenant = session.get(Tenant, tid)
            with tenant_scope(session, tid):
                return catalogue_for(session, tenant, node_id=app.state.settings.node_id)

    assert len(published()) == 1
    client.delete(url(resource_id), headers=alpha_admin)
    assert published() == []


def test_withdrawal_marks_the_catalogue_dirty(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app, discoverability=Discoverability.CATALOGUE)
    tid = tenant_id(app)
    with Session(app.state.engine) as session:
        for pending in session.exec(select(CataloguePush)).all():
            session.delete(pending)
        session.commit()

    client.delete(url(resource_id), headers=alpha_admin)

    with Session(app.state.engine) as session:
        assert session.get(CataloguePush, tid) is not None


# --- what the owner can see, and cannot do ----------------------------------------------


def test_the_owner_can_still_see_it_and_when_it_goes(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """D25 reads "gone to everyone", and `decide()` enforces exactly that. This is the
    organisation looking at its own registry — without it, deleting the wrong resource
    is undiscoverable."""
    resource_id = make_resource(app)
    client.delete(url(resource_id), headers=alpha_admin)

    shown = client.get(url(resource_id), headers=alpha_admin)
    assert shown.status_code == 200
    assert shown.json()["status"] == "withdrawn"
    assert shown.json()["purge_after"] is not None

    listed = client.get("/v1/t/alpha/resources", headers=alpha_admin).json()
    assert [item["status"] for item in listed] == ["withdrawn"]


def test_a_withdrawn_resource_cannot_be_patched(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """409, not 404: the caller can see it, so pretending it is absent would be the
    worse answer. Editing would resurrect something already scheduled to go."""
    resource_id = make_resource(app)
    client.delete(url(resource_id), headers=alpha_admin)

    response = client.patch(url(resource_id), headers=alpha_admin, json={"title": "Back"})
    assert response.status_code == 409
    assert response.json()["reason"] == "conflict"


def test_a_withdrawn_resource_cannot_be_uploaded_to(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app)
    client.delete(url(resource_id), headers=alpha_admin)
    assert (
        client.put(url(resource_id, "/data"), headers=alpha_admin, content=b"new").status_code
        == 409
    )


def test_deleting_twice_is_a_conflict_not_a_silent_success(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """Answering "done" would hide that the purge date was set by the first call and
    has not moved."""
    resource_id = make_resource(app)
    assert client.delete(url(resource_id), headers=alpha_admin).status_code == 200
    assert client.delete(url(resource_id), headers=alpha_admin).status_code == 409


def test_a_member_may_not_delete(client: TestClient, app, alpha_user: dict[str, str]) -> None:
    resource_id = make_resource(app)
    assert client.delete(url(resource_id), headers=alpha_user).status_code == 403
    assert row(app, resource_id).status is ResourceStatus.ACTIVE


def test_another_org_may_not_delete(client: TestClient, app, beta_user: dict[str, str]) -> None:
    resource_id = make_resource(app)
    assert client.delete(url(resource_id), headers=beta_user).status_code == 403


def test_the_deletion_is_logged(client: TestClient, app, alpha_admin: dict[str, str]) -> None:
    resource_id = make_resource(app)
    client.delete(url(resource_id), headers=alpha_admin)

    entry = log_rows(app)[-1]
    assert entry.action == "resource_delete"
    assert entry.decision == "allow"
    assert entry.resource_id == resource_id


# --- stage two: purge ---------------------------------------------------------------------


def test_nothing_is_purged_before_its_date(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app)
    client.delete(url(resource_id), headers=alpha_admin)

    assert purge_due(app.state.engine, app.state.storage) == []
    assert bytes_exist(app, resource_id)
    assert row(app, resource_id) is not None


def test_the_purge_removes_the_bytes_and_the_row(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    resource_id = make_resource(app)
    client.delete(url(resource_id), headers=alpha_admin)

    later = datetime.now(UTC) + timedelta(days=31)
    purged = purge_due(app.state.engine, app.state.storage, now=later)

    assert len(purged) == 1
    assert not bytes_exist(app, resource_id)
    assert row(app, resource_id) is None


def test_an_active_resource_is_never_purged(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """The query is status **and** date, and both halves have to be load-bearing.

    An ordinary active resource has `purge_after = None`, so the date alone already
    excludes it — which means a test using only those would pass with the status check
    deleted, and the first version of this one did exactly that.

    So this also builds the state the status check actually defends against: a resource
    that is still `active` but has somehow acquired a past `purge_after`. It should not
    arise, and that is the point — if a bug or a hand-edited row produces it, the purge
    must refuse to delete live data rather than reason from the date alone.
    """
    kept = make_resource(app, slug="kept")
    going = make_resource(app, slug="going", content=b"x")
    client.delete(url(going), headers=alpha_admin)

    mislabelled = make_resource(app, slug="mislabelled", content=b"live data")
    with Session(app.state.engine) as session, all_tenants(session):
        resource = session.get(Resource, mislabelled)
        resource.purge_after = datetime.now(UTC) - timedelta(days=1)
        session.add(resource)
        session.commit()
        assert resource.status is ResourceStatus.ACTIVE

    purge_due(app.state.engine, app.state.storage, now=datetime.now(UTC) + timedelta(days=31))

    assert row(app, kept) is not None
    assert bytes_exist(app, kept)
    assert row(app, going) is None
    assert row(app, mislabelled) is not None, "an active resource is never purged"


def test_the_access_log_survives_the_purge(
    client: TestClient, app, alpha_admin: dict[str, str], alpha_user: dict[str, str]
) -> None:
    """Invariant 14. `AccessLog.resource_id` is deliberately not a foreign key, which is
    what lets an entry outlive the row it names — an audit answers what happened, not
    what is still there."""
    resource_id = make_resource(app)
    client.get(url(resource_id, "/data"), headers=alpha_user)
    client.delete(url(resource_id), headers=alpha_admin)

    before = len(log_rows(app))
    purge_due(app.state.engine, app.state.storage, now=datetime.now(UTC) + timedelta(days=31))

    after = log_rows(app)
    assert len(after) == before
    assert any(entry.resource_id == resource_id for entry in after)
    assert row(app, resource_id) is None, "the entries now name something that is gone"


def test_the_purge_removes_staging_leftovers(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """A crashed upload leaves a part-written file outside the resource's directory. If
    the purge only removed the directory, those bytes would outlive the thing they
    belonged to — the one outcome a retention promise cannot survive."""
    resource_id = make_resource(app)
    tid = tenant_id(app)
    leftover = staging_location(f"{resource_id}-crashed")
    with app.state.storage.open(tid, leftover, "wb") as handle:
        handle.write(b"half an upload")

    client.delete(url(resource_id), headers=alpha_admin)
    purge_due(app.state.engine, app.state.storage, now=datetime.now(UTC) + timedelta(days=31))

    assert not app.state.storage.exists(tid, leftover)


def test_the_purge_crosses_tenants(client: TestClient, app, realm: FixtureRealm) -> None:
    """It is a job about the node, not about one organisation — which is why it is the
    case `all_tenants()` was written for."""
    alpha = make_resource(app)
    beta_tid = tenant_id(app, "beta")
    with Session(app.state.engine) as session, tenant_scope(session, beta_tid):
        beta_resource = Resource(
            tenant_id=beta_tid,
            slug="beta-set",
            kind=ResourceKind.DATASET,
            shape=Shape.FILE,
            title="Beta",
            theme=Theme.PROCESSING,
            classification=Classification.NON_SENSITIVE,
            status=ResourceStatus.WITHDRAWN,
            withdrawn_at=datetime.now(UTC) - timedelta(days=40),
            purge_after=datetime.now(UTC) - timedelta(days=10),
        )
        session.add(beta_resource)
        session.commit()
        session.refresh(beta_resource)
        beta_id = beta_resource.id

    client.delete(url(alpha), headers=bearer(realm.user_token("alpha.admin")))
    purged = purge_due(
        app.state.engine, app.state.storage, now=datetime.now(UTC) + timedelta(days=31)
    )

    assert len(purged) == 2
    assert row(app, alpha) is None
    assert row(app, beta_id) is None


def test_a_purged_resource_frees_its_slug(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """Registering the same slug is blocked while the resource is merely withdrawn —
    its row still holds the unique constraint — and works once it is purged. Worth
    pinning because the alternative, silently reusing the withdrawn row, would hand
    back a resource with someone else's access log attached to it."""
    resource_id = make_resource(app)
    client.delete(url(resource_id), headers=alpha_admin)

    body = {
        "slug": "batch-7",
        "kind": "dataset",
        "title": "Batch 7, again",
        "theme": "processing",
        "classification": "non-sensitive",
    }
    assert client.post("/v1/t/alpha/resources", headers=alpha_admin, json=body).status_code == 409

    purge_due(app.state.engine, app.state.storage, now=datetime.now(UTC) + timedelta(days=31))
    again = client.post("/v1/t/alpha/resources", headers=alpha_admin, json=body)
    assert again.status_code == 201
    assert again.json()["id"] != str(resource_id)
