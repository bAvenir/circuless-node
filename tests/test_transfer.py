"""Serving a dataset's bytes (N8), against a real issuer.

The first suite where `decide()` and the AccessLog are exercised through HTTP rather
than in isolation, so most of what is worth asserting is about the *order* things happen
in — authorise, log, then touch the disk — and about what a refused caller can learn,
which should be nothing at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, col, select
from starlette.testclient import TestClient

from circuless_node import access_log, transfer
from circuless_node.app import create_public_app
from circuless_node.models import AccessLog, AgreementCache, Resource, Tenant
from circuless_node.settings import Settings
from circuless_node.storage import resource_location
from circuless_node.subject import PrincipalType, Subject
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

FILE_BYTES = b"spectrum,intensity\n400,0.21\n401,0.24\n"


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
    """Migrated, not `create_all` — the AccessLog's append-only triggers live in the
    migration, and `record_bytes` writes through them on every transfer here."""
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


def tenant_id(app, slug: str) -> uuid.UUID:
    with Session(app.state.engine) as session:
        return session.exec(select(Tenant).where(Tenant.slug == slug)).one().id


def make_resource(
    app,
    *,
    slug: str = "batch-7",
    tenant: str = "alpha",
    visibility: Visibility = Visibility.ORG,
    shape: Shape = Shape.FILE,
    status: ResourceStatus = ResourceStatus.ACTIVE,
    storage_path: str | None = "batch-7.csv",
    content: bytes | None = FILE_BYTES,
    objects: dict[str, bytes] | None = None,
) -> uuid.UUID:
    """A registered resource, with its bytes already on disk.

    Built directly rather than through the API because N19 (upload) does not exist yet —
    this is the half of the lifecycle N8 can be tested against today.
    """
    tid = tenant_id(app, tenant)
    with Session(app.state.engine) as session, tenant_scope(session, tid):
        resource = Resource(
            tenant_id=tid,
            slug=slug,
            kind=ResourceKind.DATASET,
            shape=shape,
            title="Recycled PET batch 7",
            theme=Theme.MATERIAL_CHARACTERISATION,
            classification=Classification.NON_SENSITIVE,
            licence="CC-BY-4.0",
            discoverability=Discoverability.HIDDEN,
            visibility=visibility,
            status=status,
            storage_path=storage_path,
        )
        session.add(resource)
        session.commit()
        session.refresh(resource)
        resource_id = resource.id

    # The node owns the layout (N19): bytes live under the resource's own id, never at
    # a provider-chosen path. Written through `resource_location` rather than spelled
    # out, so this fixture cannot drift from what the handlers read.
    storage = app.state.storage
    if shape is Shape.FILE and content is not None:
        with storage.open(tid, resource_location(resource_id, storage_path or slug), "wb") as f:
            f.write(content)
    for relative, payload in (objects or {}).items():
        with storage.open(tid, resource_location(resource_id, relative), "wb") as f:
            f.write(payload)
    return resource_id


def grant(
    app,
    *,
    provider: str,
    consumer: str,
    resource_id: uuid.UUID | None,
    actions: str = "read",
    status: str = "accepted",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> None:
    """An agreement as N7's pull would have cached it."""
    with Session(app.state.engine) as session:
        session.add(
            AgreementCache(
                id=uuid.uuid4(),
                provider_org=provider,
                consumer_org=consumer,
                resource_id=resource_id,
                actions=actions,
                valid_from=valid_from or datetime.now(UTC) - timedelta(days=1),
                valid_until=valid_until,
                status=status,
            )
        )
        session.commit()


def log_rows(app) -> list[AccessLog]:
    with Session(app.state.engine) as session, all_tenants(session):
        return list(session.exec(select(AccessLog).order_by(col(AccessLog.ts))).all())


def url(resource_id: uuid.UUID, tenant: str = "alpha", suffix: str = "") -> str:
    return f"/v1/t/{tenant}/resources/{resource_id}/data{suffix}"


# --- the owning organisation ----------------------------------------------------------


def test_a_member_of_the_owning_org_reads_the_bytes(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    resource_id = make_resource(app)
    response = client.get(url(resource_id), headers=alpha_user)

    assert response.status_code == 200
    assert response.content == FILE_BYTES
    assert response.headers["content-length"] == str(len(FILE_BYTES))
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert "batch-7.csv" in response.headers["content-disposition"]


def test_the_transfer_is_logged_with_the_bytes_sent(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    resource_id = make_resource(app)
    client.get(url(resource_id), headers=alpha_user)

    entry = log_rows(app)[-1]
    assert entry.action == "read"
    assert entry.decision == "allow"
    assert entry.resource_id == resource_id
    assert entry.acting_org == "alpha"
    assert entry.bytes == len(FILE_BYTES)


def test_a_stranger_is_refused_and_the_refusal_is_logged(
    client: TestClient, app, beta_user: dict[str, str]
) -> None:
    resource_id = make_resource(app, visibility=Visibility.ORG)
    response = client.get(url(resource_id), headers=beta_user)

    assert response.status_code == 403
    assert response.json()["reason"] == "not_permitted"

    entry = log_rows(app)[-1]
    assert entry.decision == "deny"
    assert entry.reason == "not_permitted"
    assert entry.bytes is None


def test_private_is_admins_only(
    client: TestClient, app, alpha_admin: dict[str, str], alpha_user: dict[str, str]
) -> None:
    resource_id = make_resource(app, visibility=Visibility.PRIVATE)
    assert client.get(url(resource_id), headers=alpha_admin).status_code == 200
    assert client.get(url(resource_id), headers=alpha_user).status_code == 403


# --- agreements (F7) ------------------------------------------------------------------


def test_an_accepted_agreement_lets_another_org_read(
    client: TestClient, app, beta_user: dict[str, str]
) -> None:
    resource_id = make_resource(app, visibility=Visibility.AGREEMENT)
    grant(app, provider="alpha", consumer="beta", resource_id=resource_id)

    response = client.get(url(resource_id), headers=beta_user)
    assert response.status_code == 200
    assert response.content == FILE_BYTES

    entry = log_rows(app)[-1]
    assert entry.acting_org == "beta", "the log must say on whose behalf it was read"


def test_without_an_agreement_the_reason_is_actionable(
    client: TestClient, app, beta_user: dict[str, str]
) -> None:
    """`no_agreement` rather than `not_permitted`: the caller can do something about it."""
    resource_id = make_resource(app, visibility=Visibility.AGREEMENT)
    response = client.get(url(resource_id), headers=beta_user)

    assert response.status_code == 403
    assert response.json()["reason"] == "no_agreement"
    assert log_rows(app)[-1].reason == "no_agreement"


def test_a_revoked_agreement_does_not_permit(
    client: TestClient, app, beta_user: dict[str, str]
) -> None:
    resource_id = make_resource(app, visibility=Visibility.AGREEMENT)
    grant(app, provider="alpha", consumer="beta", resource_id=resource_id, status="revoked")
    assert client.get(url(resource_id), headers=beta_user).status_code == 403


def test_an_expired_agreement_does_not_permit(
    client: TestClient, app, beta_user: dict[str, str]
) -> None:
    resource_id = make_resource(app, visibility=Visibility.AGREEMENT)
    grant(
        app,
        provider="alpha",
        consumer="beta",
        resource_id=resource_id,
        valid_from=datetime.now(UTC) - timedelta(days=10),
        valid_until=datetime.now(UTC) - timedelta(days=1),
    )
    assert client.get(url(resource_id), headers=beta_user).status_code == 403


def test_an_invoke_only_agreement_does_not_permit_read(
    client: TestClient, app, beta_user: dict[str, str]
) -> None:
    resource_id = make_resource(app, visibility=Visibility.AGREEMENT)
    grant(app, provider="alpha", consumer="beta", resource_id=resource_id, actions="invoke")
    assert client.get(url(resource_id), headers=beta_user).status_code == 403


# --- what a refused caller can learn: nothing -----------------------------------------


def test_the_decision_happens_before_the_filesystem(
    client: TestClient, app, beta_user: dict[str, str]
) -> None:
    """A refused caller gets the same 403 whether or not the bytes exist.

    This is the ordering assertion the module is built around. If the handler checked
    the file first, this would be a 404 — and the difference between 403 and 404 would
    tell an outsider which registered resources have data behind them.
    """
    present = make_resource(app, slug="has-data")
    absent = make_resource(app, slug="no-data", storage_path=None, content=None)

    assert client.get(url(present), headers=beta_user).status_code == 403
    assert client.get(url(absent), headers=beta_user).status_code == 403


def test_a_withdrawn_resource_is_not_found_and_is_logged(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """D25: gone to everyone, including the organisation that owns it. `not_found`
    rather than `not_permitted`, so it is indistinguishable from one that never was."""
    resource_id = make_resource(app, status=ResourceStatus.WITHDRAWN)
    response = client.get(url(resource_id), headers=alpha_admin)

    assert response.status_code == 404
    assert response.json()["reason"] == "not_found"
    assert log_rows(app)[-1].reason == "not_found"


def test_a_resource_that_never_existed_is_not_logged(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """Nothing to record it against, and logging it would let anyone fill a tenant's
    log by guessing UUIDs."""
    assert client.get(url(uuid.uuid4()), headers=alpha_admin).status_code == 404
    assert log_rows(app) == []


def test_a_node_principal_is_refused(client: TestClient, app, realm: FixtureRealm) -> None:
    """D14, the second lock. N2 refuses node tokens already; this is the one that still
    holds if a node's key leaks."""
    resource_id = make_resource(app, visibility=Visibility.PUBLIC)
    response = client.get(url(resource_id), headers=bearer(realm.node_token()))
    assert response.status_code == 403
    assert response.json()["reason"] == "node_principal_not_permitted"


# --- buckets ---------------------------------------------------------------------------


def test_a_bucket_returns_a_manifest(client: TestClient, app, alpha_user: dict[str, str]) -> None:
    resource_id = make_resource(
        app,
        shape=Shape.BUCKET,
        storage_path="campaign-3",
        content=None,
        objects={"meta.csv": b"a,b\n", "spectra/run-07.csv": b"400,0.21\n"},
    )
    body = client.get(url(resource_id), headers=alpha_user).json()

    assert body["truncated"] is False
    assert [item["path"] for item in body["objects"]] == ["meta.csv", "spectra/run-07.csv"]
    assert body["objects"][0]["size"] == 4
    assert body["objects"][1]["size"] == 9


def test_an_empty_bucket_is_not_an_error(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    """Registered before anything was uploaded is a legitimate state."""
    resource_id = make_resource(app, shape=Shape.BUCKET, storage_path="campaign-4", content=None)
    response = client.get(url(resource_id), headers=alpha_user)
    assert response.status_code == 200
    assert response.json() == {"objects": [], "truncated": False}


def test_each_bucket_object_is_decided_and_logged_on_its_own(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    """Not because objects can differ — nothing in the model expresses that — but so the
    log shows *which* of a campaign someone pulled, and so a revocation takes effect
    mid-campaign rather than the manifest acting as a bearer token."""
    resource_id = make_resource(
        app,
        shape=Shape.BUCKET,
        storage_path="campaign-3",
        content=None,
        objects={"meta.csv": b"a,b\n", "spectra/run-07.csv": b"400,0.21\n"},
    )
    client.get(url(resource_id), headers=alpha_user)  # the manifest
    first = client.get(url(resource_id, suffix="/meta.csv"), headers=alpha_user)
    second = client.get(url(resource_id, suffix="/spectra/run-07.csv"), headers=alpha_user)

    assert first.content == b"a,b\n"
    assert second.content == b"400,0.21\n"

    entries = log_rows(app)
    assert len(entries) == 3, "manifest and each object are separate decisions"
    assert [e.bytes for e in entries[1:]] == [4, 9]
    assert all(e.resource_id == resource_id for e in entries)


def test_a_bucket_object_needs_the_same_permission(
    client: TestClient, app, beta_user: dict[str, str]
) -> None:
    resource_id = make_resource(
        app,
        shape=Shape.BUCKET,
        storage_path="campaign-3",
        content=None,
        objects={"meta.csv": b"a,b\n"},
    )
    assert client.get(url(resource_id, suffix="/meta.csv"), headers=beta_user).status_code == 403


def test_a_path_on_a_file_resource_is_a_shape_error(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    resource_id = make_resource(app)
    response = client.get(url(resource_id, suffix="/anything.csv"), headers=alpha_user)
    assert response.status_code == 400
    assert response.json()["reason"] == "unsupported"


# --- path confinement (H3, SR-3.2.3) ---------------------------------------------------


@pytest.mark.parametrize(
    "traversal",
    [
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "spectra%2f..%2f..%2f..%2fnode.db",
        "%2f etc%2fpasswd",
        "https:%2f%2fevil.example%2fx",
    ],
)
def test_a_traversal_is_refused_with_a_reason_code_not_a_crash(
    client: TestClient, app, alpha_user: dict[str, str], traversal: str
) -> None:
    """Before N8 nothing could reach `Storage.resolve`'s refusal, so it had no handler
    and would have surfaced as 500 `internal_error` while `path_not_allowed` sat unused
    in the enum."""
    resource_id = make_resource(
        app,
        shape=Shape.BUCKET,
        storage_path="campaign-3",
        content=None,
        objects={"meta.csv": b"a,b\n"},
    )
    response = client.get(url(resource_id, suffix=f"/{traversal}"), headers=alpha_user)

    assert response.status_code == 400, response.text
    assert response.json()["reason"] == "path_not_allowed"


def test_confinement_is_checked_after_the_decision(
    client: TestClient, app, beta_user: dict[str, str]
) -> None:
    """A caller with no rights gets 403, not a path error — whether `..` is legal is not
    an authorisation question, and answering it first would leak the grammar."""
    resource_id = make_resource(app, shape=Shape.BUCKET, storage_path="campaign-3", content=None)
    response = client.get(url(resource_id, suffix="/%2e%2e%2f%2e%2e%2fnode.db"), headers=beta_user)
    assert response.status_code == 403


# --- no Range in the beta ----------------------------------------------------------------


def test_range_is_declined_rather_than_ignored_silently(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    """`Accept-Ranges: none` is said out loud so a client does not attempt to resume and
    silently re-download the whole file believing it appended."""
    resource_id = make_resource(app)
    response = client.get(url(resource_id), headers={**alpha_user, "Range": "bytes=0-3"})

    assert response.status_code == 200
    assert response.content == FILE_BYTES
    assert response.headers["accept-ranges"] == "none"


# --- a file that was registered but never uploaded ----------------------------------------


def test_a_resource_with_no_bytes_tells_the_owner_so(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    resource_id = make_resource(app, storage_path=None, content=None)
    response = client.get(url(resource_id), headers=alpha_user)

    assert response.status_code == 404
    assert response.json()["reason"] == "not_found"
    assert "uploaded" in response.json()["detail"]


def test_tenancy_holds_across_the_transfer_routes(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    """Alpha's resource id, requested under Beta's tenant, is not found — N4's filter,
    not a check written here."""
    resource_id = make_resource(app, tenant="alpha")
    assert client.get(url(resource_id, tenant="beta"), headers=alpha_user).status_code == 404


def test_an_abandoned_stream_leaves_bytes_null(app) -> None:
    """N11 defines null `bytes` as "granted, did not complete". This is the first code
    that can produce it: `record_bytes` sits after the yield loop and deliberately not
    in a `finally`, so closing the generator never reaches it. A partial count in the
    same column as a completed one would be worse than nothing — an auditor totalling a
    month of transfers would be quietly wrong.

    Driven against the generator rather than through `TestClient`, which cannot model
    this: its ASGI transport runs the application to completion before handing back a
    stream, so an abandoned read there still records the full count. Under uvicorn a
    disconnected client closes the generator, which is exactly what `.close()` does
    here.
    """
    resource_id = make_resource(app, content=b"x" * (200 * 1024))
    tid = tenant_id(app, "alpha")
    subject = Subject(
        sub="11111111-1111-1111-1111-111111111111",
        principal_type=PrincipalType.USER,
        actor="test-ui",
        org_ids={"alpha"},
        admin_of={"alpha"},
    )

    def entry_for() -> uuid.UUID:
        return access_log.record(
            app.state.engine,
            tenant_id=tid,
            request_id=uuid.uuid4().hex,
            action="read",
            subject=subject,
            allowed=True,
            resource_id=resource_id,
            acting_org="alpha",
        )

    def bytes_of(entry_id: uuid.UUID) -> int | None:
        with Session(app.state.engine) as session, tenant_scope(session, tid):
            return session.get(AccessLog, entry_id).bytes

    abandoned = entry_for()
    stream = transfer._stream(
        app.state.storage,
        app.state.engine,
        tid,
        resource_location(resource_id, "batch-7.csv"),
        abandoned,
    )
    next(stream)
    stream.close()
    assert bytes_of(abandoned) is None

    # And the completing case does record, so the assertion above is about the
    # abandonment and not about `record_bytes` being broken.
    completed = entry_for()
    for _ in transfer._stream(
        app.state.storage,
        app.state.engine,
        tid,
        resource_location(resource_id, "batch-7.csv"),
        completed,
    ):
        pass
    assert bytes_of(completed) == 200 * 1024
