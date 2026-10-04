"""The access log (N11, invariant 12).

Four things are worth proving here, and only four:

1. **Every management decision is recorded** — allow *and* deny. The denials are the
   half a logging bug loses, because they are the ones written on a path that then
   raises.
2. **Append-only is real.** Against a migrated database, not a `create_all` one; see
   `harness/migrated.py` for why that distinction is the whole point.
3. **One organisation cannot read another's log** — by N4's filter, not by a handler
   remembering.
4. **No names, no emails** (D31), checked against tokens from the real fixture realm,
   where a user genuinely has a first name and a surname to leak.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import DatabaseError
from sqlmodel import Session, col, select
from starlette.testclient import TestClient

from circuless_node import access_log
from circuless_node.access_log import REQUEST_ID_HEADER
from circuless_node.app import create_public_app
from circuless_node.models import AccessLog, Tenant
from circuless_node.settings import Settings
from circuless_node.subject import PrincipalType, Subject
from circuless_node.tenancy import all_tenants, tenant_scope

from .harness.keycloak import FixtureRealm
from .harness.migrated import migrated_engine

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
    """Built by the migrations, so the append-only triggers exist.

    Every other suite uses `create_all`, which would leave the triggers out and quietly
    turn the append-only tests below into assertions about nothing.
    """
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
def alpha_member(realm: FixtureRealm) -> dict[str, str]:
    return bearer(realm.user_token("alpha.user"))


@pytest.fixture
def beta_admin(realm: FixtureRealm) -> dict[str, str]:
    """`admins.only` is the fixture realm's admin of Beta — see `tests/realm`."""
    return bearer(realm.user_token("admins.only"))


DATASET = {
    "slug": "batch-7",
    "kind": "dataset",
    "title": "Recycled PET batch 7",
    "theme": "material-characterisation",
    "classification": "non-sensitive",
}


def rows(app, tenant_slug: str | None = None) -> list[AccessLog]:
    with Session(app.state.engine) as session, all_tenants(session):
        statement = select(AccessLog).order_by(col(AccessLog.ts))
        if tenant_slug is not None:
            tenant = session.exec(select(Tenant).where(Tenant.slug == tenant_slug)).one()
            statement = statement.where(col(AccessLog.tenant_id) == tenant.id)
        return list(session.exec(statement).all())


# --- every decision is recorded, both ways (invariant 12) -----------------------------


def test_an_allowed_management_action_is_recorded(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    assert (
        client.post("/v1/t/alpha/resources", headers=alpha_admin, json=DATASET).status_code == 201
    )

    entry = rows(app)[-1]
    assert entry.action == "resource_register"
    assert entry.decision == "allow"
    assert entry.reason is None
    assert entry.acting_org == "alpha"
    assert entry.principal_type == PrincipalType.USER.value


def test_a_refused_management_action_is_recorded(
    client: TestClient, app, alpha_member: dict[str, str]
) -> None:
    """The one a logging bug loses.

    The refusal raises, the handler's session is rolled back, and an entry written
    inside that transaction would vanish with it — so this asserts the property the
    engine-not-session signature in `access_log.record` exists to give.
    """
    response = client.post("/v1/t/alpha/resources", headers=alpha_member, json=DATASET)
    assert response.status_code == 403

    entry = rows(app)[-1]
    assert entry.decision == "deny"
    assert entry.reason == "not_permitted"
    # Nothing of the caller's was accepted, so there is no organisation they acted as.
    assert entry.acting_org is None


def test_a_cross_org_attempt_is_recorded_against_the_tenant_it_targeted(
    client: TestClient, app, beta_admin: dict[str, str]
) -> None:
    """Beta's admin reaching into Alpha appears in *Alpha's* log.

    That is the point of the field being the tenant rather than the caller: the
    organisation entitled to ask "who has been trying my data" is the one whose data it
    is.
    """
    assert client.get("/v1/t/alpha/resources", headers=beta_admin).status_code == 403

    alpha_rows = rows(app, "alpha")
    assert [e.decision for e in alpha_rows] == ["deny"]
    assert rows(app, "beta") == []


def test_an_unknown_tenant_is_not_logged(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """No tenant, no entry. "Someone asked about a tenant we do not host" is a fact
    about this node, not a decision about anyone's resources — and there is no
    organisation it could belong to."""
    assert client.get("/v1/t/nowhere/resources", headers=alpha_admin).status_code == 404
    assert rows(app) == []


def test_reading_the_log_appears_in_the_log(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """Self-referential on purpose: "who has been reading the access log" is exactly
    the question an access log should answer about itself.

    Asserted against the table rather than against the response, because *whether a read
    returns its own entry* is a property of the driver and not of this node. The entry is
    written on a second connection, so on SQLite — where pysqlite runs a SELECT outside
    any transaction — the read sees it, and on Postgres — where the session holds a
    snapshot from its first statement — it does not. Both are correct, neither is worth
    pinning, and a test that picked one would fail the day `DATABASE_URL` changes.
    """
    assert client.get("/v1/t/alpha/access-log", headers=alpha_admin).status_code == 200

    recorded = rows(app)
    assert [entry.action for entry in recorded] == ["access_log_read"]
    assert recorded[0].decision == "allow"
    assert recorded[0].acting_org == "alpha"


# --- append-only (invariant 12), against the triggers ---------------------------------


def test_the_triggers_exist_at_all(app) -> None:
    """Guards the guard. If this fixture ever reverts to `create_all`, every append-only
    test below would pass while asserting nothing, and this is what would notice."""
    with app.state.engine.connect() as connection:
        names = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
    assert "access_log_append_only_update" in names
    assert "access_log_append_only_delete" in names


def one_entry(app) -> tuple[uuid.UUID, uuid.UUID]:
    with Session(app.state.engine) as session:
        tenant = session.exec(select(Tenant).where(Tenant.slug == "alpha")).one()
    subject = Subject(
        sub="11111111-1111-1111-1111-111111111111",
        principal_type=PrincipalType.USER,
        actor="circuless-ui",
        org_ids={"alpha"},
        admin_of={"alpha"},
    )
    entry_id = access_log.record(
        app.state.engine,
        tenant_id=tenant.id,
        request_id="req-1",
        action="read",
        subject=subject,
        allowed=True,
        acting_org="alpha",
    )
    return entry_id, tenant.id


def test_bytes_can_be_set_exactly_once(app) -> None:
    entry_id, tenant_id = one_entry(app)

    access_log.record_bytes(app.state.engine, entry_id, tenant_id, 4096)
    with Session(app.state.engine) as session, tenant_scope(session, tenant_id):
        assert session.get(AccessLog, entry_id).bytes == 4096

    # A second call is a no-op rather than a 500 — `record_bytes` checks before writing,
    # so a bug upstream does not break an otherwise-successful transfer.
    access_log.record_bytes(app.state.engine, entry_id, tenant_id, 9999)
    with Session(app.state.engine) as session, tenant_scope(session, tenant_id):
        assert session.get(AccessLog, entry_id).bytes == 4096


def test_the_database_refuses_a_second_bytes_write(app) -> None:
    """Past `record_bytes`, straight at the table — the guarantee is the trigger, and
    the Python check above is only manners."""
    entry_id, tenant_id = one_entry(app)
    access_log.record_bytes(app.state.engine, entry_id, tenant_id, 10)

    with pytest.raises(DatabaseError, match="append-only"), app.state.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE access_log SET bytes = 20 WHERE id = ?", (entry_id.hex,))


def test_the_database_refuses_a_delete(app) -> None:
    entry_id, _tenant_id = one_entry(app)
    with pytest.raises(DatabaseError, match="append-only"), app.state.engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM access_log WHERE id = ?", (entry_id.hex,))


@pytest.mark.parametrize(
    "column", [column.name for column in AccessLog.__table__.columns if column.name != "bytes"]
)
def test_the_database_refuses_a_change_to_any_other_column(app, column: str) -> None:
    """Driven off the live model, so adding a column to `AccessLog` without adding it to
    the migration's `_IMMUTABLE_COLUMNS` fails here — which is the only thing keeping
    that hand-written list in step (SQLite cannot compare rows; Postgres can, and needs
    no list)."""
    entry_id, _tenant_id = one_entry(app)
    replacement = uuid.uuid4().hex if column.endswith("id") else "tampered"

    with pytest.raises(DatabaseError, match="append-only"), app.state.engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE access_log SET {column} = ? WHERE id = ?", (replacement, entry_id.hex)
        )


# --- tenancy, and what is never stored ------------------------------------------------


def test_an_org_cannot_read_another_orgs_log(
    client: TestClient, alpha_admin: dict[str, str], beta_admin: dict[str, str]
) -> None:
    assert (
        client.post("/v1/t/alpha/resources", headers=alpha_admin, json=DATASET).status_code == 201
    )

    assert client.get("/v1/t/beta/access-log", headers=alpha_admin).status_code == 403

    # And Beta's own log, correctly scoped, holds only Beta's decisions — not Alpha's.
    beta = client.get("/v1/t/beta/access-log", headers=beta_admin).json()["entries"]
    assert all(entry["action"] == "access_log_read" for entry in beta)


def test_a_member_cannot_read_the_log(client: TestClient, alpha_member: dict[str, str]) -> None:
    """Membership lets you consume under `visibility=org`; reading the log is admin-only
    (N18), and the refusal is itself recorded."""
    assert client.get("/v1/t/alpha/access-log", headers=alpha_member).status_code == 403


def test_the_log_never_holds_a_name_or_an_email(
    client: TestClient, app, realm: FixtureRealm, alpha_admin: dict[str, str]
) -> None:
    """D31, against a real token belonging to a user who has both.

    The claims are absent from a node-audienced token, so there is nothing to copy — but
    this asserts the outcome rather than the mechanism, because the mechanism is a realm
    setting somebody could change.
    """
    client.post("/v1/t/alpha/resources", headers=alpha_admin, json=DATASET)

    stored = " ".join(
        str(value)
        for entry in rows(app)
        for value in entry.model_dump().values()
        if value is not None
    ).lower()
    for forbidden in ("@", "alpha admin", "firstname", "lastname"):
        assert forbidden not in stored, f"the access log leaked {forbidden!r}"

    # And the subject is the opaque Keycloak id, which is what makes the entry useful
    # without being identifying on its own.
    assert uuid.UUID(rows(app)[-1].subject_sub)


# --- the request id --------------------------------------------------------------------


def test_every_response_carries_a_request_id(
    client: TestClient, alpha_admin: dict[str, str]
) -> None:
    for response in (
        client.get("/v1/whoami", headers=alpha_admin),
        client.get("/v1/t/alpha/resources", headers=alpha_admin),
        client.get("/v1/t/alpha/resources"),  # 401, and it needs an id most of all
    ):
        assert response.headers.get(REQUEST_ID_HEADER)


def test_the_logged_id_is_the_one_the_caller_was_given(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    response = client.post("/v1/t/alpha/resources", headers=alpha_admin, json=DATASET)
    assert rows(app)[-1].request_id == response.headers[REQUEST_ID_HEADER]


def test_an_inbound_request_id_is_ignored(
    client: TestClient, app, alpha_admin: dict[str, str]
) -> None:
    """Never trusted from the caller. An id someone else chooses can be repeated, or
    collided with another caller's, and finding every entry for one request is the one
    thing this field has to be good for."""
    forged = "f" * 32
    response = client.post(
        "/v1/t/alpha/resources",
        headers={**alpha_admin, REQUEST_ID_HEADER: forged},
        json=DATASET,
    )
    assert response.headers[REQUEST_ID_HEADER] != forged
    assert rows(app)[-1].request_id != forged


def test_each_request_gets_its_own_id(client: TestClient, alpha_admin: dict[str, str]) -> None:
    seen = {
        client.get("/v1/t/alpha/resources", headers=alpha_admin).headers[REQUEST_ID_HEADER]
        for _ in range(5)
    }
    assert len(seen) == 5
