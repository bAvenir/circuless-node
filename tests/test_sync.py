"""Cloud synchronisation (N7).

`tick()` talks to a `CloudTransport`, so these drive it with a fake. That is a deliberate
limit: what the node **does** is asserted here, and what it puts **on the wire** waits for
the cross-repo integration tests (Q1). The Cloud API is in a private repository and this
one is public, so the node's CI cannot run it — the same constraint that makes
`tests/realm/` a fixture rather than a copy of the production realm.

Until Q1 runs, the paths and payloads in `sync.HttpCloud` are a proposal that C5 and C6
have to match, and nothing here would notice if they did not.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from circuless_node import sync
from circuless_node.models import (
    AgreementCache,
    CataloguePush,
    OrgMap,
    Resource,
    Tenant,
)
from circuless_node.sync import CloudError, SyncState, tick
from circuless_node.tenancy import tenant_scope
from circuless_node.vocabularies import (
    Classification,
    Discoverability,
    ResourceKind,
    ResourceStatus,
    Shape,
    Theme,
    Visibility,
)

NOW = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.UTC)
ALPHA = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


class FakeCloud:
    """Records what the node sent, and answers what the test told it to."""

    def __init__(self, feed: dict[str, Any] | None = None) -> None:
        self.feed = feed if feed is not None else {"org_map": [], "agreements": []}
        self.pushed: list[tuple[str, list[dict]]] = []
        self.heartbeats: list[str | None] = []
        self.fail_on: set[str] = set()

    def push_catalogue(self, org_slug: str, records: list[dict[str, Any]]) -> None:
        if "push" in self.fail_on:
            raise CloudError("push refused")
        self.pushed.append((org_slug, records))

    def fetch_sync(self) -> dict[str, Any]:
        if "pull" in self.fail_on:
            raise CloudError("cloud unreachable")
        return self.feed

    def heartbeat(self, version: str | None) -> None:
        if "heartbeat" in self.fail_on:
            raise CloudError("heartbeat refused")
        self.heartbeats.append(version)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as opened:
        opened.add(Tenant(id=ALPHA, org_id=uuid.uuid4(), group_path="/orgs/alpha", slug="alpha"))
        opened.commit()
        yield opened


def add_resource(session: Session, **overrides) -> Resource:
    fields = {
        "tenant_id": ALPHA,
        "slug": "batch-7",
        "kind": ResourceKind.DATASET,
        "shape": Shape.FILE,
        "title": "Recycled PET batch 7",
        "theme": Theme.MATERIAL_CHARACTERISATION,
        "classification": Classification.NON_SENSITIVE,
        "licence": "CC-BY-4.0",
        "discoverability": Discoverability.CATALOGUE,
        "visibility": Visibility.ORG,
        **overrides,
    }
    resource = Resource(**fields)
    with tenant_scope(session, ALPHA):
        session.add(resource)
        session.commit()
        session.refresh(resource)
    return resource


def agreement(**overrides) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "provider_org": "alpha",
        "consumer_org": "beta",
        "resource_id": None,
        "actions": ["read"],
        "valid_from": "2026-09-01T00:00:00+00:00",
        "valid_until": None,
        "status": "accepted",
        **overrides,
    }


# --- pushing the catalogue ------------------------------------------------------------


def test_a_marked_tenant_is_pushed_and_unmarked(session: Session) -> None:
    add_resource(session)
    session.add(CataloguePush(tenant_id=ALPHA))
    session.commit()

    cloud = FakeCloud()
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)

    assert [slug for slug, _ in cloud.pushed] == ["alpha"]
    assert session.exec(select(CataloguePush)).all() == []


def test_nothing_is_pushed_when_nothing_changed(session: Session) -> None:
    add_resource(session)
    cloud = FakeCloud()
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)
    assert cloud.pushed == []


def test_a_failed_push_keeps_the_mark(session: Session) -> None:
    """A mark is cheap and a lost push is not.

    Clearing it on failure would leave the Cloud's copy wrong until the next unrelated
    edit to that tenant — which could be never.
    """
    add_resource(session)
    session.add(CataloguePush(tenant_id=ALPHA))
    session.commit()

    cloud = FakeCloud()
    cloud.fail_on = {"push"}
    state = SyncState()
    tick(session, cloud, state, node_id="test-node", now=NOW)

    assert session.exec(select(CataloguePush)).all() != []
    assert state.consecutive_push_failures == 1


def test_only_published_resources_are_sent(session: Session) -> None:
    """Filtered by discoverability, never by visibility.

    `hidden` is the default (NFR4) and means not advertised anywhere. Sending visibility
    to a catalogue would publish the access policy of every resource to everyone who can
    search.
    """
    add_resource(session, slug="published", discoverability=Discoverability.CATALOGUE)
    add_resource(session, slug="secret", discoverability=Discoverability.HIDDEN)
    add_resource(
        session,
        slug="restricted",
        discoverability=Discoverability.CATALOGUE,
        visibility=Visibility.PRIVATE,
    )
    session.add(CataloguePush(tenant_id=ALPHA))
    session.commit()

    cloud = FakeCloud()
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)

    _slug, records = cloud.pushed[0]
    titles = {record["@id"].rsplit("/", 1)[-1] for record in records}
    assert len(titles) == 2, "hidden resources must not reach the catalogue"
    assert "visibility" not in str(records)


def test_a_withdrawn_resource_is_absent_from_the_push(session: Session) -> None:
    """How the Cloud learns to withdraw its copy (D25) — an absence, like a revocation."""
    add_resource(session, slug="gone", status=ResourceStatus.WITHDRAWN)
    session.add(CataloguePush(tenant_id=ALPHA))
    session.commit()

    cloud = FakeCloud()
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)
    assert cloud.pushed[0][1] == []


def test_a_mark_for_a_vanished_tenant_is_dropped(session: Session) -> None:
    """Otherwise it retries forever, failing the whole pass each time."""
    session.add(CataloguePush(tenant_id=uuid.uuid4()))
    session.commit()

    cloud = FakeCloud()
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)
    assert session.exec(select(CataloguePush)).all() == []


# --- pulling agreements and the org map -------------------------------------------------


def test_a_pull_caches_agreements_and_the_org_map(session: Session) -> None:
    cloud = FakeCloud(
        {
            "org_map": [
                {
                    "slug": "alpha",
                    "org_id": str(uuid.uuid4()),
                    "group_path": "/orgs/alpha",
                    "display_name": "Alpha Ltd",
                }
            ],
            "agreements": [agreement()],
        }
    )
    state = SyncState()
    tick(session, cloud, state, node_id="test-node", now=NOW)

    assert len(session.exec(select(AgreementCache)).all()) == 1
    assert session.exec(select(OrgMap)).one().slug == "alpha"
    assert state.agreements_cached == 1


def test_a_pull_replaces_rather_than_merges(session: Session) -> None:
    """The failure mode this cache must not have.

    A revocation is an *absence* from the feed. Merging would never see one, so the node
    would go on honouring an agreement the provider had withdrawn.
    """
    keeper, revoked = agreement(), agreement()
    cloud = FakeCloud({"org_map": [], "agreements": [keeper, revoked]})
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)
    assert len(session.exec(select(AgreementCache)).all()) == 2

    cloud.feed = {"org_map": [], "agreements": [keeper]}
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)

    cached = session.exec(select(AgreementCache)).all()
    assert [str(row.id) for row in cached] == [keeper["id"]]


def test_actions_round_trip(session: Session) -> None:
    cloud = FakeCloud({"org_map": [], "agreements": [agreement(actions=["invoke", "read"])]})
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)

    cached = session.exec(select(AgreementCache)).one()
    assert cached.permits("read") and cached.permits("invoke")
    assert not cached.permits("delete")


def test_a_naive_timestamp_is_read_as_utc(session: Session) -> None:
    """A naive value would compare wrongly against an aware `now`, shifting an
    agreement's validity window by the local offset — quietly, and only for nodes not
    running in UTC."""
    cloud = FakeCloud({"org_map": [], "agreements": [agreement(valid_from="2026-09-01T00:00:00")]})
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)
    assert session.exec(select(AgreementCache)).one().valid_from.tzinfo is not None


def test_a_null_resource_id_means_every_resource(session: Session) -> None:
    """How a blanket agreement is expressed. Null, not a sentinel UUID: null is the thing
    SQL can answer questions about."""
    cloud = FakeCloud({"org_map": [], "agreements": [agreement(resource_id=None)]})
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)
    assert session.exec(select(AgreementCache)).one().resource_id is None


# --- the Cloud being down (F16) ------------------------------------------------------------


def test_a_failed_pull_leaves_the_cache_intact(session: Session) -> None:
    """The whole point of caching agreements rather than asking per request.

    A control-plane outage must not become a data-plane one: the node goes on serving
    from what it has.
    """
    cloud = FakeCloud({"org_map": [], "agreements": [agreement()]})
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)

    cloud.fail_on = {"pull"}
    state = SyncState()
    tick(session, cloud, state, node_id="test-node", now=NOW)

    assert len(session.exec(select(AgreementCache)).all()) == 1
    assert state.consecutive_pull_failures == 1
    assert state.last_pull_at is None


def test_a_failure_does_not_raise(session: Session) -> None:
    """The loop's job is to try again in thirty seconds. A node that stopped syncing
    because the Cloud blinked would need a restart to recover."""
    cloud = FakeCloud()
    cloud.fail_on = {"pull"}
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)  # does not raise


def test_failures_accumulate_and_a_success_clears_them(session: Session) -> None:
    cloud = FakeCloud()
    state = SyncState()
    cloud.fail_on = {"pull"}
    tick(session, cloud, state, node_id="test-node", now=NOW)
    tick(session, cloud, state, node_id="test-node", now=NOW)
    assert state.consecutive_pull_failures == 2

    cloud.fail_on = set()
    tick(session, cloud, state, node_id="test-node", now=NOW)
    assert state.consecutive_pull_failures == 0
    assert state.last_pull_error is None


def test_an_error_is_truncated(session: Session) -> None:
    """It reaches /metrics and the logs. An upstream error string can carry a URL, a
    hostname, or a fragment of something that should not travel."""
    state = SyncState()
    state.pull_failed("x" * 5000, now=NOW)
    assert len(state.last_pull_error) <= 200


# --- the heartbeat --------------------------------------------------------------------------


def test_the_heartbeat_is_sent_last(session: Session) -> None:
    """It says "this node completed a pass", which is more useful than "started one"."""
    cloud = FakeCloud()
    tick(session, cloud, SyncState(), node_id="test-node", version="0.2.0", now=NOW)
    assert cloud.heartbeats == ["0.2.0"]


def test_no_heartbeat_when_the_pass_failed(session: Session) -> None:
    cloud = FakeCloud()
    cloud.fail_on = {"pull"}
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)
    assert cloud.heartbeats == []


# --- staleness reporting ----------------------------------------------------------------------


def test_metrics_report_minus_one_before_the_first_success() -> None:
    """Not zero. Zero reads as "synced just now", which is the opposite of the truth."""
    text = sync.metrics_text(SyncState(), now=NOW)
    assert "circuless_node_sync_age_seconds -1" in text


def test_metrics_report_the_age_of_the_cache() -> None:
    state = SyncState()
    state.pull_succeeded(agreements=3, now=NOW - dt.timedelta(minutes=5))
    text = sync.metrics_text(state, now=NOW)
    assert "circuless_node_sync_age_seconds 300" in text
    assert "circuless_node_agreements_cached 3" in text


def test_metrics_report_consecutive_failures() -> None:
    state = SyncState()
    state.pull_failed("nope", now=NOW)
    state.pull_failed("nope", now=NOW)
    assert "circuless_node_pull_failures_total 2" in sync.metrics_text(state, now=NOW)


# --- what the internal socket exposes -------------------------------------------------------


def test_healthz_stays_200_when_the_cache_is_stale(settings) -> None:
    """F16, and a correction to what this docstring used to say.

    A node enforcing from a stale cache is doing exactly what it was designed to do. A
    503 would have an orchestrator remove it during the very Cloud outage the cache
    exists to survive — and remove every node at once, since they would all be stale
    together.
    """
    from starlette.testclient import TestClient

    from circuless_node.app import create_internal_app

    app = create_internal_app(settings)
    app.state.sync_state.pull_failed("cloud unreachable", now=NOW)

    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    # And still no body: staleness is not something to publish to anything on the socket.
    assert response.content == b""


def test_metrics_expose_the_sync_state(settings) -> None:
    from starlette.testclient import TestClient

    from circuless_node.app import create_internal_app

    app = create_internal_app(settings)
    app.state.sync_state.pull_succeeded(agreements=2, now=NOW - dt.timedelta(seconds=90))

    body = TestClient(app).get("/metrics").text
    assert "circuless_node_sync_age_seconds" in body
    assert "circuless_node_agreements_cached 2" in body


def test_staleness_is_on_the_internal_socket_only(settings) -> None:
    """R8. When a node is running degraded is operational detail, not something to
    publish through the gateway."""
    from starlette.testclient import TestClient

    from circuless_node.app import create_public_app

    assert TestClient(create_public_app(settings)).get("/metrics").status_code == 404


def test_a_node_whose_certificate_is_not_registered_still_serves(
    session: Session, settings
) -> None:
    """The state of every fresh install, before an operator registers the certificate.

    `CloudCredentials.token()` raises its own error type, not `CloudError`. If that
    escaped, the sync task would die and take the two servers down with it through
    `asyncio.gather` — so a node would refuse to start until someone completed a manual
    step that `circuless-node check` exists to diagnose while it is running.
    """
    from circuless_node.identity import CloudCredentials, load_or_create_keypair
    from circuless_node.sync import HttpCloud

    # Nothing is listening on cloud_api_url, and no certificate is registered anywhere.
    cloud = HttpCloud(settings, CloudCredentials(settings, load_or_create_keypair(settings)))

    state = SyncState()
    tick(session, cloud, state, node_id="test-node", now=NOW)  # must not raise

    assert state.consecutive_pull_failures == 1
    assert state.last_pull_at is None


def test_a_public_record_is_never_pushed(session: Session) -> None:
    """`public` is not available in the beta (§5.2) and the Cloud's catalogue refuses it.

    `resources.py` no longer lets one be created, so this can only be a record from
    before that rule — written directly here, as a node upgraded in place would have.
    Pushing it would be refused, and a refused push stops `tick()` before the pull, so
    one stale record would wedge agreement synchronisation indefinitely.
    """
    add_resource(session, slug="legacy", discoverability=Discoverability.PUBLIC)
    session.add(CataloguePush(tenant_id=ALPHA))
    session.commit()

    cloud = FakeCloud()
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)

    assert cloud.pushed[0][1] == [], "a public record must not reach the Cloud"


# --- a failed push must not stop the pull ---------------------------------------------


def test_a_failed_push_does_not_stop_the_pull(session: Session) -> None:
    """The one that matters.

    Push and pull fail for different reasons and cost different things. Until this was
    fixed, a push the Cloud refused returned before the pull — so a single record the
    Cloud would not accept, or a tenant an operator had removed from the node registry,
    stopped agreements arriving indefinitely while the node went on enforcing from a
    cache nobody was updating. A node may be behind on what it advertises; it must not
    silently fall behind on what it permits.
    """
    add_resource(session)
    session.add(CataloguePush(tenant_id=ALPHA))
    session.commit()

    cloud = FakeCloud({"org_map": [], "agreements": [agreement()]})
    cloud.fail_on = {"push"}
    state = SyncState()
    tick(session, cloud, state, node_id="test-node", now=NOW)

    assert len(session.exec(select(AgreementCache)).all()) == 1, "the pull must still run"
    assert state.consecutive_push_failures == 1
    assert state.consecutive_pull_failures == 0
    assert state.last_pull_at == NOW


def test_a_node_failing_only_its_pushes_is_not_stale(session: Session) -> None:
    """Staleness means enforcement staleness.

    Reporting one number for both exchanges would say this node is stale when its
    agreements are current and only its catalogue entries are behind — which is the
    opposite of the truth, and the number someone pages on.
    """
    cloud = FakeCloud()
    cloud.fail_on = {"push"}
    state = SyncState()
    add_resource(session)
    session.add(CataloguePush(tenant_id=ALPHA))
    session.commit()

    tick(session, cloud, state, node_id="test-node", now=NOW)

    assert state.seconds_since_pull(NOW) == 0
    assert "circuless_node_sync_age_seconds 0" in sync.metrics_text(state, now=NOW)
    assert "circuless_node_push_failures_total 1" in sync.metrics_text(state, now=NOW)


def test_a_failed_pull_still_skips_the_heartbeat(session: Session) -> None:
    """It says "this node completed a pass", and a pass without agreements did not."""
    cloud = FakeCloud()
    cloud.fail_on = {"pull"}
    tick(session, cloud, SyncState(), node_id="test-node", now=NOW)
    assert cloud.heartbeats == []


def test_a_failed_heartbeat_does_not_discard_a_good_pull(session: Session) -> None:
    """Nothing this node decides depends on the heartbeat. Failing the pass over it
    would hide a pull that worked, and the pull is the part that matters."""
    cloud = FakeCloud({"org_map": [], "agreements": [agreement()]})
    cloud.fail_on = {"heartbeat"}
    state = SyncState()
    tick(session, cloud, state, node_id="test-node", now=NOW)

    assert state.last_pull_at == NOW
    assert state.consecutive_pull_failures == 0
    assert len(session.exec(select(AgreementCache)).all()) == 1


def test_a_pull_success_does_not_erase_a_push_failure(session: Session) -> None:
    """One error field for both exchanges would have the good news erase the bad.

    A node whose pushes are failing and whose pulls are fine is a real state, and the
    push error is the only record of why its catalogue entries are going stale.
    """
    add_resource(session)
    session.add(CataloguePush(tenant_id=ALPHA))
    session.commit()

    cloud = FakeCloud()
    cloud.fail_on = {"push"}
    state = SyncState()
    tick(session, cloud, state, node_id="test-node", now=NOW)

    assert state.last_push_error is not None
    assert state.last_pull_error is None


async def test_the_loop_records_an_unexpected_error_rather_than_dying(tmp_path) -> None:
    """The loop's own safety net, which was broken and untested.

    `tick()` swallows CloudError, so anything reaching the loop's handler is a bug — and
    the handler called a method that no longer existed, so it would have raised
    AttributeError and killed the sync task. Through `asyncio.gather` in `_serve`, that
    takes both servers with it: a node that stops answering because of a malformed sync
    feed.
    """
    import asyncio

    from circuless_node.settings import Settings
    from circuless_node.sync import sync_loop

    settings = Settings(  # type: ignore[call-arg]
        node_id="test-node",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'n.db'}",
    )
    state = SyncState()
    # `engine=None` makes `_one_pass` raise something that is not a CloudError, which is
    # exactly the path the handler exists for.
    task = asyncio.create_task(sync_loop(settings, None, state, interval=0.01))
    await asyncio.sleep(0.2)
    assert not task.done(), "the loop died instead of recording the error"
    task.cancel()

    assert state.consecutive_pull_failures > 0
    assert "sync loop" in (state.last_pull_error or "")
