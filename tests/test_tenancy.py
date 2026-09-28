"""Tenant isolation (N4, R10).

The tenant-owned model here is defined by the tests, not by the node: N4 is the machinery,
and N5's resource registry is its first real consumer. Using a test model keeps the two
diffs separate and proves the filter works on *anything* that subclasses `TenantOwned`,
which is the actual guarantee — a table opts in by its shape, not by appearing on a list
someone has to remember to update.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap
import uuid

import pytest
from sqlalchemy import StaticPool
from sqlmodel import Field, Session, SQLModel, create_engine, select

from circuless_node.errors import NodeError
from circuless_node.models import Tenant, TenantOwned
from circuless_node.tenancy import (
    TenancyNotScopedError,
    all_tenants,
    tenant_by_slug,
    tenant_scope,
)


class Widget(TenantOwned, table=True):
    """A tenant-owned table, standing in for Resource, ServiceCredential and AccessLog."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str


class NodeFact(SQLModel, table=True):
    """Node-global, standing in for AgreementCache, OrgMap and NodeIdentity.

    Deliberately *not* a `TenantOwned`: filtering these would break sync, because the
    agreements a node enforces belong to no single tenant (R10).
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    note: str


ALPHA = uuid.uuid4()
BETA = uuid.uuid4()


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Tenant(id=ALPHA, org_id=uuid.uuid4(), group_path="/orgs/alpha", slug="alpha"),
                Tenant(id=BETA, org_id=uuid.uuid4(), group_path="/orgs/beta", slug="beta"),
                NodeFact(note="belongs to the node, not to anyone"),
            ]
        )
        with all_tenants(session):
            session.add_all(
                [
                    Widget(tenant_id=ALPHA, name="alpha-one"),
                    Widget(tenant_id=ALPHA, name="alpha-two"),
                    Widget(tenant_id=BETA, name="beta-one"),
                ]
            )
            session.commit()
        yield session


# ------------------------------------------------------------------- reads are scoped


def test_a_scoped_session_sees_only_its_own_rows(session: Session) -> None:
    with tenant_scope(session, ALPHA):
        names = {w.name for w in session.exec(select(Widget)).all()}
    assert names == {"alpha-one", "alpha-two"}


def test_the_other_tenant_sees_its_own(session: Session) -> None:
    with tenant_scope(session, BETA):
        names = {w.name for w in session.exec(select(Widget)).all()}
    assert names == {"beta-one"}


def test_a_handler_cannot_reach_another_tenants_row_by_id(session: Session) -> None:
    """The case a hand-written filter usually misses: fetching by primary key.

    A handler that trusts an id from the URL and calls `session.get` would otherwise
    happily return another organisation's row.
    """
    with all_tenants(session):
        beta_widget = session.exec(select(Widget).where(Widget.name == "beta-one")).one()
        beta_id = beta_widget.id
    session.expunge_all()

    with tenant_scope(session, ALPHA):
        assert session.exec(select(Widget).where(Widget.id == beta_id)).first() is None


def test_scope_does_not_leak_out_of_the_block(session: Session) -> None:
    with tenant_scope(session, ALPHA):
        pass
    with pytest.raises(TenancyNotScopedError):
        session.exec(select(Widget)).all()


def test_nested_scopes_restore_the_outer_one(session: Session) -> None:
    with tenant_scope(session, ALPHA):
        with tenant_scope(session, BETA):
            assert {w.name for w in session.exec(select(Widget)).all()} == {"beta-one"}
        assert {w.name for w in session.exec(select(Widget)).all()} == {
            "alpha-one",
            "alpha-two",
        }


# --------------------------------------------------------- unscoped fails, loudly


def test_an_unscoped_query_raises_rather_than_returning_everything(session: Session) -> None:
    """The whole point. If forgetting to bind silently returned every tenant's rows, the
    mistake would look exactly like working code."""
    with pytest.raises(TenancyNotScopedError):
        session.exec(select(Widget)).all()


def test_crossing_tenants_is_possible_but_has_to_be_said_out_loud(session: Session) -> None:
    """The purge job (N20) is about the node, not about one organisation."""
    with all_tenants(session):
        assert len(session.exec(select(Widget)).all()) == 3


# ------------------------------------------------------- node-global stays unfiltered


def test_node_global_tables_are_not_filtered(session: Session) -> None:
    """R10. Applying the tenant filter to these would break sync."""
    assert len(session.exec(select(NodeFact)).all()) == 1

    with tenant_scope(session, ALPHA):
        assert len(session.exec(select(NodeFact)).all()) == 1, (
            "a node-global table must be readable inside a tenant scope too"
        )


def test_the_tenant_table_itself_is_readable_unscoped(session: Session) -> None:
    """`Tenant` is what the filter keys on. If reading it required already knowing the
    tenant id, no route could ever resolve a slug."""
    assert len(session.exec(select(Tenant)).all()) == 2


# ------------------------------------------------------------------ writes are scoped


def test_writing_another_tenants_row_is_refused(session: Session) -> None:
    """`with_loader_criteria` only touches SELECT, so the write side needs its own check.

    Without it a handler could insert a row under someone else's tenant_id and then never
    see it again — a confusing way to find out.
    """
    with tenant_scope(session, ALPHA), pytest.raises(TenancyNotScopedError):
        session.add(Widget(tenant_id=BETA, name="smuggled"))
        session.commit()
    session.rollback()


def test_moving_a_row_to_another_tenant_is_refused(session: Session) -> None:
    with tenant_scope(session, ALPHA):
        widget = session.exec(select(Widget).where(Widget.name == "alpha-one")).one()
        widget.tenant_id = BETA
        with pytest.raises(TenancyNotScopedError):
            session.commit()
    session.rollback()


def test_writing_your_own_tenants_row_is_fine(session: Session) -> None:
    with tenant_scope(session, ALPHA):
        session.add(Widget(tenant_id=ALPHA, name="alpha-three"))
        session.commit()
        assert len(session.exec(select(Widget)).all()) == 3


# ----------------------------------------------------------------- resolving a slug


def test_a_slug_resolves_to_its_tenant(session: Session) -> None:
    assert tenant_by_slug(session, "alpha").id == ALPHA


def test_an_unknown_slug_is_not_found(session: Session) -> None:
    """404, not a distinct code: whether an organisation exists elsewhere in CIRCULess is
    not this node's to disclose."""
    with pytest.raises(NodeError) as raised:
        tenant_by_slug(session, "gamma")
    assert raised.value.status_code == 404


def test_the_filter_is_active_without_importing_tenancy(tmp_path) -> None:
    """The listeners must already be registered by the time anyone can make a session.

    They attach at import of `circuless_node.tenancy`, which nothing in the node would
    otherwise import — so `db` imports it for that side effect. If that import is ever
    tidied away as unused, the node runs with tenant isolation silently off.

    This has to run in a **fresh interpreter**. Every other test in this file imports
    `tenancy` at module level, which registers the listeners as a side effect, so an
    in-process version of this check passes whether or not `db` does its job — it did,
    when first written, which is the reason for the subprocess.
    """
    probe = textwrap.dedent(
        """
        import sys, uuid
        from sqlmodel import Field, Session, SQLModel, create_engine, select

        # Deliberately only `db`. Importing tenancy here would defeat the test.
        from circuless_node.db import create_db_engine
        from circuless_node.models import TenantOwned
        from circuless_node.settings import Settings

        assert "circuless_node.tenancy" in sys.modules, (
            "importing circuless_node.db did not pull in tenancy, so no session in the "
            "node has a tenant filter attached"
        )

        class Probe(TenantOwned, table=True):
            id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

        engine = create_db_engine(
            Settings(node_id="probe", database_url=sys.argv[1], data_dir="/tmp")
        )
        SQLModel.metadata.create_all(engine)

        with Session(engine) as session:
            try:
                session.exec(select(Probe)).all()
            except RuntimeError:
                sys.exit(0)
        sys.exit("an unscoped query returned rows instead of raising")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, f"sqlite:///{tmp_path / 'probe.db'}"],
        capture_output=True,
        text=True,
        cwd=pathlib.Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0, result.stderr or result.stdout
