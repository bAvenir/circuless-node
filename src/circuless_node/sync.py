"""Cloud synchronisation (N7, F3, F4, F7, F14, F16).

Three exchanges, all initiated by the node. **The Cloud never reaches into a node** — a
node behind a partner's firewall has no inbound ports at all (D10), so every
conversation starts here.

| | |
|---|---|
| push | the tenant's whole DCAT catalogue, when N5 marked it dirty |
| pull | provider-side agreements and the org map, every 30 s |
| heartbeat | "still here", with the running version |

## Enforcement continues when the Cloud is down (F16)

A failed pull changes nothing. The cache stays, `decide()` keeps answering from it, and
the node goes on serving. That is the point of caching agreements rather than asking per
request (§3.7): a control-plane outage must not become a data-plane outage.

The window is bounded anyway — nobody can obtain a fresh token once Keycloak is
unreachable, and tokens live five minutes — so the exposure is a revocation that arrives
late, not indefinite access.

**Staleness is reported, not enforced.** `/metrics` carries the age of the cache and the
consecutive failure count; `/healthz` stays 200. A node enforcing from a stale cache is
doing exactly what it was built to do, and answering 503 would have an orchestrator
remove it during precisely the outage it was designed to survive.

## A pull replaces; it does not merge

A revocation is an *absence* from the feed. Merging would never notice one, so the node
would keep honouring an agreement the provider had withdrawn — the one failure mode this
cache must not have. The whole provider-side set arrives on every pull and replaces what
is there, inside one transaction, so a half-applied feed is never enforced.

## Why `tick()` takes a transport

The Cloud API lives in a **private** repository and this one is public, so the node's CI
cannot run it — the same constraint that makes `tests/realm/` a fixture rather than a copy
of the production realm. `tick()` therefore talks to a `CloudTransport`, and the tests
drive it with a fake: what the node *does* is asserted here, and what it puts **on the
wire** is asserted by the cross-repo integration tests (Q1). Until those run, the wire
format below is a proposal the Cloud's C5 and C6 have to match.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from sqlmodel import Session, col, delete, select

from . import dcat
from .identity import CloudCredentials, load_or_create_keypair
from .models import AgreementCache, CataloguePush, OrgMap, Resource, Tenant
from .settings import Settings
from .tenancy import tenant_scope
from .vocabularies import Discoverability, ResourceStatus

#: Design §4.8. Short enough that a revocation lands quickly, long enough that fifty
#: nodes do not become a load problem for one Cloud.
SYNC_INTERVAL_SECONDS = 30.0

#: Resources a node publishes. `hidden` is the default and means "not advertised
#: anywhere" (NFR4), so it never reaches the Cloud — the catalogue filters on
#: discoverability and never on visibility, and this is the node's half of that.
#:
#: `public` is **not** here. It is not available in the beta (design §5.2), the Cloud's
#: catalogue refuses a record carrying it, and `resources.py` no longer lets one be
#: created. A record from before that rule would otherwise be pushed, refused, and —
#: because a failed push stops `tick()` before the pull — would wedge agreement
#: synchronisation indefinitely over a discovery problem.
PUBLISHED = (Discoverability.CATALOGUE,)


# --- what the node knows about its own syncing -------------------------------------------


def _redacted(error: str) -> str:
    """Truncated. It reaches the logs but never a response body — an upstream error
    string can carry a URL, a hostname or a fragment of something that should not
    travel."""
    return error[:200]


@dataclass
class SyncState:
    """Liveness of the two exchanges, held in memory.

    **Push and pull are tracked separately, because they matter differently.** The pull
    carries agreements, which is what `decide()` enforces from; the push carries
    catalogue metadata, which only affects what other people can discover. Discovery
    going stale is cosmetic. Enforcement going stale is not.

    So `last_pull_at` is what staleness means, and a node whose pushes are all failing
    while its pulls succeed is **not** stale — it is a node with a publishing problem.
    Reporting one number for both would have said the opposite.

    In memory by decision, with one consequence worth knowing: a restart resets it, so a
    node that comes back up during a Cloud outage reports "never pulled" while it is in
    fact enforcing from a cache that survived in the database. The age of the process,
    not the age of the data. `/metrics` names the counter accordingly.
    """

    last_pull_at: dt.datetime | None = None
    last_push_at: dt.datetime | None = None
    last_attempt_at: dt.datetime | None = None
    last_pull_error: str | None = None
    last_push_error: str | None = None
    consecutive_pull_failures: int = 0
    consecutive_push_failures: int = 0
    agreements_cached: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def pull_succeeded(self, *, agreements: int, now: dt.datetime) -> None:
        with self._lock:
            self.last_pull_at = now
            self.last_attempt_at = now
            self.consecutive_pull_failures = 0
            # Cleared by its own success only. A pull that worked says nothing about a
            # push that did not, and one field for both would erase the other's news.
            self.last_pull_error = None
            self.agreements_cached = agreements

    def push_succeeded(self, *, now: dt.datetime) -> None:
        with self._lock:
            self.last_push_at = now
            self.last_attempt_at = now
            self.consecutive_push_failures = 0
            self.last_push_error = None

    def pull_failed(self, error: str, *, now: dt.datetime) -> None:
        with self._lock:
            self.last_attempt_at = now
            self.last_pull_error = _redacted(error)
            self.consecutive_pull_failures += 1

    def push_failed(self, error: str, *, now: dt.datetime) -> None:
        with self._lock:
            self.last_attempt_at = now
            self.last_push_error = _redacted(error)
            self.consecutive_push_failures += 1

    def seconds_since_pull(self, now: dt.datetime) -> float | None:
        if self.last_pull_at is None:
            return None
        return (now - self.last_pull_at).total_seconds()


# --- talking to the Cloud -------------------------------------------------------------------


class CloudTransport(Protocol):
    """What `tick()` needs from the Cloud. A Protocol so the tests can supply a fake."""

    def push_catalogue(self, org_slug: str, records: list[dict[str, Any]]) -> None: ...

    def fetch_sync(self) -> dict[str, Any]: ...

    def heartbeat(self, version: str | None) -> None: ...


class CloudError(RuntimeError):
    """The Cloud was unreachable, or answered something we cannot use."""


class HttpCloud:
    """The real transport.

    Every path says `self` rather than naming this node. The node's identity comes from
    its token, so there is nothing in a request for another node to be named in — the
    same reasoning that put the Cloud's heartbeat at `/v1/nodes/self/heartbeat` (C8).
    """

    def __init__(
        self, settings: Settings, credentials: CloudCredentials, *, timeout: float = 15.0
    ) -> None:
        self._base = settings.cloud_api_url.rstrip("/")
        self._credentials = credentials
        self._timeout = timeout

    def _call(self, method: str, path: str, payload: dict | None = None) -> Any:
        try:
            response = httpx.request(
                method,
                f"{self._base}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {self._credentials.token()}"},
                timeout=self._timeout,
            )
        except Exception as failure:  # noqa: BLE001 — every transport failure is the same here
            raise CloudError(f"{method} {path}: {failure}") from failure

        if response.status_code >= 400:
            # The body is the Cloud's reason code, which is safe and useful: "this node
            # does not host that tenant" is the message an operator needs.
            raise CloudError(f"{method} {path} -> {response.status_code} {response.text[:200]}")
        return response.json() if response.content else None

    def push_catalogue(self, org_slug: str, records: list[dict[str, Any]]) -> None:
        self._call("PUT", f"/v1/nodes/self/catalogue/{org_slug}", {"records": records})

    def fetch_sync(self) -> dict[str, Any]:
        return self._call("GET", "/v1/nodes/self/sync")

    def heartbeat(self, version: str | None) -> None:
        self._call("POST", "/v1/nodes/self/heartbeat", {"version": version})


# --- one pass ---------------------------------------------------------------------------------


def tick(
    session: Session,
    cloud: CloudTransport,
    state: SyncState,
    *,
    node_id: str,
    version: str | None = None,
    now: dt.datetime | None = None,
) -> None:
    """Push, pull, heartbeat. One pass, no loop, no sleeping — so a test can call it.

    Order matters. The push goes first so that a catalogue change registered a moment ago
    is visible in the Cloud before the pull that might bring back an agreement about it.
    The heartbeat goes last: it says "this node completed a pass", which is more useful
    than "this node started one".

    **A failed push does not stop the pull.** They fail for different reasons and cost
    different things — see `SyncState`.

    Nothing here raises: the loop's job is to run again in thirty seconds, and a node
    that stopped syncing because the Cloud was briefly down would need a restart to
    recover.
    """
    now = now or dt.datetime.now(dt.UTC)

    # Each exchange is attempted independently. A failed push used to return before the
    # pull, so one record the Cloud would not accept — a `public` discoverability, or a
    # tenant an operator removed from the node registry — stopped agreements arriving
    # indefinitely, while the node went on enforcing from a cache nobody was updating.
    # That inverts F16's priority: a node may be behind on what it advertises, and must
    # not silently fall behind on what it permits.
    try:
        push_dirty_catalogues(session, cloud, node_id=node_id)
        state.push_succeeded(now=now)
    except CloudError as failure:
        state.push_failed(str(failure), now=now)

    try:
        agreements = pull_sync_feed(session, cloud, now=now)
        state.pull_succeeded(agreements=agreements, now=now)
    except CloudError as failure:
        state.pull_failed(str(failure), now=now)
        # No heartbeat: it says "this node completed a pass", and this one did not.
        return

    try:
        cloud.heartbeat(version)
    except CloudError as failure:
        # Recorded and otherwise tolerated. A heartbeat is the Cloud's view of our
        # liveness; nothing this node decides depends on it, and failing the pass over
        # it would hide a pull that worked.
        state.push_failed(str(failure), now=now)


def push_dirty_catalogues(session: Session, cloud: CloudTransport, *, node_id: str) -> None:
    """Send the catalogue of every tenant N5 marked, then clear the mark.

    Cleared only after the push succeeds. A mark is cheap and a lost push is not: the
    Cloud's copy would stay wrong until the next unrelated edit, which could be never.
    """
    for pending in session.exec(select(CataloguePush)).all():
        tenant = session.get(Tenant, pending.tenant_id)
        if tenant is None:
            # The tenant went away between the mark and the push. Nothing to send, and
            # leaving the row would retry forever.
            session.delete(pending)
            continue

        cloud.push_catalogue(tenant.slug, catalogue_for(session, tenant, node_id=node_id))
        session.delete(pending)
        session.commit()


def catalogue_for(session: Session, tenant: Tenant, *, node_id: str) -> list[dict[str, Any]]:
    """Every DCAT record this tenant publishes.

    Filtered by `discoverability`, never by `visibility`: the first decides what may be
    *advertised*, the second who may *read*, and sending the second to a catalogue would
    publish the access policy of every resource to everyone who can search.

    Withdrawn resources are excluded, which is how the Cloud learns to withdraw its copy
    (D25) — an absence, for the same reason a revoked agreement is one.
    """
    with tenant_scope(session, tenant.id):
        resources = session.exec(
            select(Resource).where(
                col(Resource.discoverability).in_(PUBLISHED),
                col(Resource.status) == ResourceStatus.ACTIVE,
            )
        ).all()
        return [
            dcat.render(resource, node_id=node_id, tenant_slug=tenant.slug)
            for resource in resources
        ]


def pull_sync_feed(session: Session, cloud: CloudTransport, *, now: dt.datetime) -> int:
    """Replace the org map and the agreement cache with what the Cloud sent.

    Replace, never merge: a revocation is an absence from the feed, and a merge would
    never see one. Both replacements happen in one transaction, so a half-applied feed is
    never enforced — and a failure part-way leaves the previous set intact, which is what
    F16 requires.

    Returns how many agreements are now cached, for the staleness metric.
    """
    feed = cloud.fetch_sync()

    session.exec(delete(OrgMap))
    for entry in feed.get("org_map", []):
        session.add(
            OrgMap(
                slug=entry["slug"],
                org_id=uuid.UUID(str(entry["org_id"])),
                group_path=entry["group_path"],
                display_name=entry.get("display_name", ""),
                synced_at=now,
            )
        )

    session.exec(delete(AgreementCache))
    agreements = feed.get("agreements", [])
    for entry in agreements:
        session.add(
            AgreementCache(
                id=uuid.UUID(str(entry["id"])),
                provider_org=entry["provider_org"],
                consumer_org=entry["consumer_org"],
                resource_id=(
                    uuid.UUID(str(entry["resource_id"])) if entry.get("resource_id") else None
                ),
                actions=",".join(sorted(entry.get("actions", []))),
                valid_from=_parse_time(entry["valid_from"]),
                valid_until=(
                    _parse_time(entry["valid_until"]) if entry.get("valid_until") else None
                ),
                status=entry["status"],
                policy=entry.get("policy"),
                synced_at=now,
            )
        )
    session.commit()
    return len(agreements)


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    # A naive timestamp from the Cloud would compare wrongly against an aware `now` and
    # silently shift an agreement's validity window by the local offset.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


# --- the loop ---------------------------------------------------------------------------------


async def sync_loop(
    settings: Settings,
    engine,  # noqa: ANN001 — a SQLAlchemy Engine
    state: SyncState,
    *,
    interval: float = SYNC_INTERVAL_SECONDS,
    version: str | None = None,
) -> None:
    """Run `tick()` forever, beside the two servers.

    A peer of the servers rather than a subordinate of one: the node is a single process
    and this is one of the three things it does. Nothing to install on a partner site,
    nothing to schedule, and it stops when the node stops.

    `tick()` is synchronous and touches the database and the network, so it runs in a
    worker thread — blocking the event loop here would stall every request for the
    duration of a Cloud timeout.
    """
    credentials = CloudCredentials(settings, load_or_create_keypair(settings))
    cloud = HttpCloud(settings, credentials)

    while True:
        try:
            await asyncio.to_thread(_one_pass, engine, cloud, state, settings, version)
        except Exception as unexpected:  # noqa: BLE001
            # `tick()` already swallows CloudError. Anything reaching here is a bug, and
            # the loop must still survive it: a node that stopped syncing because of one
            # malformed feed would need a restart nobody knows to perform.
            # Counted against the pull: an unexpected error means no agreements
            # arrived, which is the consequential half.
            state.pull_failed(f"sync loop: {unexpected}", now=dt.datetime.now(dt.UTC))
        await asyncio.sleep(interval)


def _one_pass(engine, cloud, state, settings, version) -> None:  # noqa: ANN001
    with Session(engine) as session:
        tick(session, cloud, state, node_id=settings.node_id, version=version)


# --- what /metrics reports ----------------------------------------------------------------------


def metrics_text(state: SyncState, *, now: dt.datetime | None = None) -> str:
    """Prometheus exposition for the sync state (F16).

    Here rather than in C13's general metrics because F16 asks for staleness
    specifically, and it is the one number that matters during a Cloud outage. C13 will
    add the rest around it.

    Push and pull are separate series. Alert on the pull: that is enforcement falling
    behind. A rising push counter means this node's catalogue entries are going stale in
    the Cloud, which is worth a ticket and not a page.
    """
    now = now or dt.datetime.now(dt.UTC)
    age = state.seconds_since_pull(now)
    lines = [
        "# HELP circuless_node_sync_age_seconds Seconds since this process last pulled "
        "agreements successfully, or -1 if it never has. Enforcement staleness.",
        "# TYPE circuless_node_sync_age_seconds gauge",
        f"circuless_node_sync_age_seconds {age if age is not None else -1:.0f}",
        "# HELP circuless_node_pull_failures_total Consecutive failed agreement pulls.",
        "# TYPE circuless_node_pull_failures_total gauge",
        f"circuless_node_pull_failures_total {state.consecutive_pull_failures}",
        "# HELP circuless_node_push_failures_total Consecutive failed catalogue pushes. "
        "Discovery, not enforcement.",
        "# TYPE circuless_node_push_failures_total gauge",
        f"circuless_node_push_failures_total {state.consecutive_push_failures}",
        "# HELP circuless_node_agreements_cached Agreements currently enforced from cache.",
        "# TYPE circuless_node_agreements_cached gauge",
        f"circuless_node_agreements_cached {state.agreements_cached}",
    ]
    return "\n".join(lines) + "\n"
