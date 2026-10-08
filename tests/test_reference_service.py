"""The reference service, behind a real node (N9, N10, N8, N19, N11).

Everything else that tests the proxy uses an `httpx.MockTransport`, which is honest
about what it covers and covers nothing about a real server: a real socket, real header
casing, a real redirect, a connection that can actually refuse. This drives the example
in `examples/reference-service/` end to end — `decide()`, the stored credential, the
proxy, a separate process, and back.

## The scenario

**Alpha** owns the data: `batch-7.csv` and a `campaign-3` bucket.
**Beta** owns the service: `csv-tools`.
An **Alpha user** downloads Alpha's CSV and has Beta's service process it.

Two permissions, and only one of them is an agreement. Downloading Alpha's own file
passes on membership — `decide()` allows the owning organisation before it ever looks at
an agreement. Invoking Beta's service needs **one accepted agreement, Beta → Alpha,
permitting `invoke`**. That is the cross-organisation exchange the platform exists for,
reduced to its smallest honest form.

## Why a subprocess on a LAN address rather than a container on loopback

The node refuses `127.0.0.1` as an upstream (`policy.check_upstream_url`) because its own
internal socket lives there, and a provider who could register it would read `/metrics`
and `/internal/authz` from outside. That guard shapes our own testing, which is a fair
sign it is real — so the service binds this machine's non-loopback address, which is
private, which the guard permits.

That makes these the first tests of the guard's **permission** side. Every other test of
it asserts a refusal, and a guard that refused everything would pass all of them.

A subprocess rather than a container keeps the suite fast and needs no image build; the
container is covered by `examples/reference-service/docker-compose.yml` and was run by
hand. The service is unmodified — the same file a partner would copy.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, col, select
from starlette.testclient import TestClient

from circuless_node.app import create_public_app
from circuless_node.credentials import load_or_create_fernet, seal
from circuless_node.models import AccessLog, AgreementCache, Resource, ServiceCredential, Tenant
from circuless_node.settings import Settings
from circuless_node.storage import resource_location
from circuless_node.tenancy import all_tenants, tenant_scope
from circuless_node.vocabularies import (
    Classification,
    Discoverability,
    ResourceKind,
    Shape,
    Theme,
    Visibility,
)

from .harness.keycloak import FixtureRealm
from .harness.migrated import migrated_engine

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "reference-service"
API_KEY = "k-reference-service-test"

ALPHA_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
BETA_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

CSV = b"sample_id,polymer,mass_g\n1,PET,4.2\n2,HDPE,3.1\n"
COLUMNS = ["sample_id", "polymer", "mass_g"]


def routable_address() -> str | None:
    """A non-loopback address of this machine that this machine can actually reach.

    Both halves matter, and the second is why this does a real round trip rather than
    just reading an interface address. A host can have `192.168.1.10` and still refuse
    to talk to itself there: macOS's local-network privacy control and a VPN client's
    network extension both do exactly that, accepting the TCP connection and then
    resetting it. An address that merely *exists* is not an address the node can use.

    Where that happens the whole module skips. The alternative — relaxing the loopback
    refusal in `policy.check_upstream_url` for tests — would mean the end-to-end run no
    longer exercises the guard it is partly there to exercise, and would put an off
    switch on a control whose entire value is that it has none.
    """
    finder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        finder.connect(("8.8.8.8", 80))
        candidate = finder.getsockname()[0]
    except OSError:
        return None
    finally:
        finder.close()
    if candidate.startswith("127."):
        return None

    # Prove it: one HTTP exchange, served on every interface, fetched via the candidate.
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 0))  # noqa: S104 — a throwaway probe, closed below
        listener.listen(1)
        port = listener.getsockname()[1]

        reached: list[bool] = []

        def answer() -> None:
            try:
                conn, _ = listener.accept()
                conn.recv(256)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                conn.close()
            except OSError:
                pass

        responder = threading.Thread(target=answer, daemon=True)
        responder.start()
        try:
            with socket.create_connection((candidate, port), timeout=2) as probe:
                probe.sendall(b"GET / HTTP/1.1\r\nHost: probe\r\n\r\n")
                reached.append(probe.recv(16).startswith(b"HTTP/1.1 200"))
        except OSError:
            reached.append(False)
        responder.join(timeout=2)

    return candidate if reached and reached[0] else None


def free_port(host: str) -> int:
    with socket.socket() as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module", params=["service", "identity"])
def upstream(request) -> str:
    """The example, running unmodified, at both tiers.

    Parametrised so the **whole scenario runs against tier 0** — the service that reads
    no CIRCULess header and does not know the platform exists. If it passes there, the
    claim that a partner changes nothing is demonstrated rather than asserted.
    """
    host = routable_address()
    if host is None:
        pytest.skip(
            "this machine cannot reach itself on a non-loopback address — a VPN client "
            "or a local-network privacy control is resetting the connection. The node "
            "refuses a loopback upstream (policy.check_upstream_url), so there is "
            "nowhere to put the service. Runs on Linux."
        )

    port = free_port(host)
    process = subprocess.Popen(  # noqa: S603 — our own example, fixed argv
        [
            sys.executable,
            "-m",
            "uvicorn",
            f"{request.param}:app",
            "--host",
            host,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=EXAMPLE,
        env={**os.environ, "REFERENCE_SERVICE_KEY": API_KEY, "REFERENCE_SERVICE_JOB_DELAY_S": "0"},
    )
    base = f"http://{host}:{port}"
    for _ in range(100):
        if process.poll() is not None:
            pytest.fail(f"the example exited with {process.returncode}")
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=1)
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    else:
        process.terminate()
        pytest.fail(f"{request.param}:app did not come up on {base}")

    yield base
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture
def node(realm: FixtureRealm, tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        issuer=realm.issuer,
        database_url=f"sqlite:///{tmp_path / 'node.db'}",
        data_dir=tmp_path / "data",
        gateway_base_url="https://node.circuless.test",
        cors_allow_origins=["http://127.0.0.1:5173"],
    )


@pytest.fixture
def app(node: Settings, upstream: str):
    """A node hosting both organisations, with the scenario already set up."""
    built = create_public_app(node)
    built.state.engine = migrated_engine(node)

    with Session(built.state.engine) as session:
        session.add(Tenant(org_id=ALPHA_ORG, group_path="/orgs/alpha", slug="alpha"))
        session.add(Tenant(org_id=BETA_ORG, group_path="/orgs/beta", slug="beta"))
        session.commit()
        alpha = session.exec(select(Tenant).where(Tenant.slug == "alpha")).one().id
        beta = session.exec(select(Tenant).where(Tenant.slug == "beta")).one().id

    def dataset(tenant_id, slug, shape, visibility=Visibility.ORG):
        with Session(built.state.engine) as session, tenant_scope(session, tenant_id):
            resource = Resource(
                tenant_id=tenant_id,
                slug=slug,
                kind=ResourceKind.DATASET,
                shape=shape,
                title=slug,
                theme=Theme.MATERIAL_CHARACTERISATION,
                classification=Classification.NON_SENSITIVE,
                licence="CC-BY-4.0",
                discoverability=Discoverability.HIDDEN,
                visibility=visibility,
                storage_path=f"{slug}.csv" if shape is Shape.FILE else None,
            )
            session.add(resource)
            session.commit()
            session.refresh(resource)
            return resource.id

    built.state.ids = {
        "alpha_tenant": alpha,
        "beta_tenant": beta,
        "csv": dataset(alpha, "batch-7", Shape.FILE),
        "bucket": dataset(alpha, "campaign-3", Shape.BUCKET),
    }

    # Alpha's bytes.
    storage = built.state.storage
    with storage.open(alpha, resource_location(built.state.ids["csv"], "batch-7.csv"), "wb") as f:
        f.write(CSV)
    for name, body in {
        "run-01.csv": CSV,
        "spectra/run-02.csv": b"wavelength,intensity\n400,0.2\n",
    }.items():
        with storage.open(alpha, resource_location(built.state.ids["bucket"], name), "wb") as f:
            f.write(body)

    # Beta's service, pointing at the running example, with the key the node will inject.
    with Session(built.state.engine) as session, tenant_scope(session, beta):
        service = Resource(
            tenant_id=beta,
            slug="csv-tools",
            kind=ResourceKind.SERVICE,
            shape=Shape.SERVICE,
            title="CSV tools",
            theme=Theme.PROCESSING,
            classification=Classification.NON_SENSITIVE,
            licence="CC-BY-4.0",
            discoverability=Discoverability.HIDDEN,
            visibility=Visibility.AGREEMENT,
            endpoint_url=upstream,
        )
        session.add(service)
        session.commit()
        session.refresh(service)
        built.state.ids["service"] = service.id
        session.add(
            ServiceCredential(
                tenant_id=beta,
                resource_id=service.id,
                scheme="header",
                header_name="x-api-key",
                secret=seal(load_or_create_fernet(node), API_KEY),
                set_by_sub="00000000-0000-0000-0000-000000000000",
            )
        )
        session.commit()
    return built


def grant_invoke(app) -> None:
    """The one agreement in the scenario: Beta lets Alpha invoke its service."""
    with Session(app.state.engine) as session:
        session.add(
            AgreementCache(
                id=uuid.uuid4(),
                provider_org="beta",
                consumer_org="alpha",
                resource_id=app.state.ids["service"],
                actions="invoke",
                valid_from=datetime.now(UTC) - timedelta(days=1),
                status="accepted",
            )
        )
        session.commit()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def alpha_user(realm: FixtureRealm) -> dict[str, str]:
    return {"Authorization": f"Bearer {realm.user_token('alpha.user')}"}


def log_rows(app) -> list[AccessLog]:
    with Session(app.state.engine) as session, all_tenants(session):
        return list(session.exec(select(AccessLog).order_by(col(AccessLog.ts))).all())


def data_url(app) -> str:
    return f"/v1/t/alpha/resources/{app.state.ids['csv']}/data"


def invoke_url(app, path: str) -> str:
    return f"/v1/t/beta/resources/{app.state.ids['service']}/invoke/{path}"


# --- the scenario ---------------------------------------------------------------------


def test_alpha_downloads_its_own_csv_and_beta_processes_it(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    """The whole thing, in the order a user would do it.

    Runs against **both tiers**, so passing at tier 0 is the proof that a service which
    has never heard of CIRCULess works unmodified behind a node.
    """
    grant_invoke(app)

    downloaded = client.get(data_url(app), headers=alpha_user)
    assert downloaded.status_code == 200, "Alpha reads its own file on membership"
    assert downloaded.content == CSV

    processed = client.post(
        invoke_url(app, "headers"), headers=alpha_user, content=downloaded.content
    )
    assert processed.status_code == 200, processed.text
    assert processed.json() == {"columns": COLUMNS, "count": 3}


def test_both_halves_are_logged_against_the_real_user(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    grant_invoke(app)
    body = client.get(data_url(app), headers=alpha_user).content
    client.post(invoke_url(app, "headers"), headers=alpha_user, content=body)

    read, invoke = log_rows(app)[-2:]
    assert (read.action, read.decision, read.acting_org) == ("read", "allow", "alpha")
    assert (invoke.action, invoke.decision, invoke.acting_org) == ("invoke", "allow", "alpha")
    assert read.subject_sub == invoke.subject_sub, "the same person, both halves"
    assert read.bytes == len(CSV)
    # Two tenants, two logs: the read is Alpha's, the invoke is Beta's.
    assert read.tenant_id == app.state.ids["alpha_tenant"]
    assert invoke.tenant_id == app.state.ids["beta_tenant"]


def test_without_the_agreement_the_service_is_never_called(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    """No agreement, no call. The refusal happens at the node; the partner's service
    never sees the request and never learns that anyone tried."""
    refused = client.post(invoke_url(app, "headers"), headers=alpha_user, content=CSV)
    assert refused.status_code == 403
    assert refused.json()["reason"] == "no_agreement"
    assert log_rows(app)[-1].decision == "deny"


def test_the_node_injects_the_credential_the_service_asked_for(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    """The service refuses without `X-API-Key`, and succeeds through the node — so the
    node is supplying it. Nothing else could."""
    grant_invoke(app)
    assert (
        client.post(invoke_url(app, "headers"), headers=alpha_user, content=CSV).status_code == 200
    )

    with Session(app.state.engine) as session, tenant_scope(session, app.state.ids["beta_tenant"]):
        session.delete(session.get(ServiceCredential, app.state.ids["service"]))
        session.commit()

    without = client.post(invoke_url(app, "headers"), headers=alpha_user, content=CSV)
    assert without.status_code == 401
    assert "X-API-Key" in without.text, "the service's own refusal, passed through"


def test_the_services_error_body_reaches_the_caller(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    """Invariant 15 allows an upstream's body through only here. A service's own error
    is the one thing the node cannot usefully restate."""
    grant_invoke(app)
    response = client.post(invoke_url(app, "headers"), headers=alpha_user, content=b"")
    assert response.status_code == 422
    assert response.json() == {"error": "the body is empty; send a CSV"}


# --- the bucket ----------------------------------------------------------------------------


def test_each_bucket_object_is_decided_and_logged_separately(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    """The property that makes a manifest safe to hand out: it is not a bearer token for
    the bucket. Agreements refresh every 30 s, so a revocation takes effect part-way
    through a campaign rather than at the next manifest."""
    bucket = f"/v1/t/alpha/resources/{app.state.ids['bucket']}/data"
    manifest = client.get(bucket, headers=alpha_user).json()
    assert [item["path"] for item in manifest["objects"]] == ["run-01.csv", "spectra/run-02.csv"]

    before = len(log_rows(app))
    for item in manifest["objects"]:
        assert client.get(f"{bucket}/{item['path']}", headers=alpha_user).status_code == 200
    assert len(log_rows(app)) == before + 2, "one entry per object, not one for the bucket"


# --- redirects and async, against a real server ----------------------------------------------


def test_a_redirect_inside_the_service_comes_back_through_the_node(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    grant_invoke(app)
    response = client.get(
        invoke_url(app, "redirect/inside"), headers=alpha_user, follow_redirects=False
    )
    assert response.status_code == 303
    # The tenant **slug**. Written as the id first time round, matching the bug in
    # `_invoke_base` — which is how a wrong expectation and wrong code agree with each
    # other. The async test below follows the rewritten URL instead of comparing it,
    # and that is what caught it.
    assert response.headers["location"] == (
        f"https://node.circuless.test/v1/t/beta/resources/{app.state.ids['service']}/invoke/headers"
    )


def test_a_redirect_out_of_the_service_is_refused(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    grant_invoke(app)
    response = client.get(
        invoke_url(app, "redirect/outside"), headers=alpha_user, follow_redirects=False
    )
    assert response.status_code == 502
    assert response.json()["reason"] == "upstream_error"


def test_an_async_job_is_passed_through_and_polled_back_through_the_node(
    client: TestClient, app, alpha_user: dict[str, str]
) -> None:
    """`202` passes through untouched, `Location` is rewritten to the node, and the
    rewritten URL **works** — which a string comparison against a mock cannot show.
    Each poll is a fresh decision and a fresh log entry."""
    grant_invoke(app)
    accepted = client.post(
        invoke_url(app, "jobs"), headers=alpha_user, content=CSV, follow_redirects=False
    )
    assert accepted.status_code == 202
    assert accepted.headers["retry-after"] == "1"

    location = accepted.headers["location"]
    assert location.startswith("https://node.circuless.test/v1/t/")

    before = len(log_rows(app))
    polled = client.get(location.removeprefix("https://node.circuless.test"), headers=alpha_user)
    assert polled.status_code == 200
    assert polled.json()["columns"] == COLUMNS
    assert len(log_rows(app)) == before + 1, "the poll is decided and logged like any call"


# --- the SSRF guard, from the permitting side --------------------------------------------------


def test_a_private_upstream_address_is_permitted(
    client: TestClient, app, alpha_user: dict[str, str], upstream: str
) -> None:
    """Every other test of the guard asserts a refusal, and a guard that refused
    everything would pass all of them. The whole scenario runs against a service on a
    private address, which is the case the guard must *allow* — partner services live on
    private networks, and D20 puts them there deliberately."""
    grant_invoke(app)
    refused = client.post(invoke_url(app, "headers"), headers=alpha_user, content=CSV)
    assert refused.status_code == 422
    assert "internal socket" in refused.text
    assert (
        client.post(invoke_url(app, "headers"), headers=alpha_user, content=CSV).status_code == 200
    )


def test_the_same_service_on_loopback_would_be_refused(
    client: TestClient, app, alpha_user: dict[str, str], upstream: str
) -> None:
    """The other side of it, on the same running service: reachable by address, refused
    by name. The node's own internal socket is on loopback, so a provider who could
    register it would read /metrics and /internal/authz from outside (R8)."""
    grant_invoke(app)
    port = upstream.rsplit(":", 1)[1]
    with Session(app.state.engine) as session, tenant_scope(session, app.state.ids["beta_tenant"]):
        service = session.get(Resource, app.state.ids["service"])
        service.endpoint_url = f"http://127.0.0.1:{port}"
        session.add(service)
        session.commit()

    refused = client.post(invoke_url(app, "headers"), headers=alpha_user, content=CSV)
    assert refused.status_code == 422
    assert "internal socket" in refused.text
