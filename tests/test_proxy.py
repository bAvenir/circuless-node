"""The service proxy (N9, invariant 10).

A proxy's contract is the request it **sends**, so most of this asserts on what the
upstream received rather than on what the consumer got back. The upstream is an
`httpx.MockTransport` injected through `app.state.upstream_transport`; a real server on
loopback is not an option here, because `check_upstream_url` refuses loopback — which is
the guard doing its job.

Endpoints use literal private addresses (`http://10.0.0.5:8080`). That is not
incidental: a literal IP is checked directly and never resolved, so these tests make no
DNS query while still running the real SSRF guard.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlmodel import Session, col, select
from starlette.testclient import TestClient

from circuless_node.app import create_public_app
from circuless_node.credentials import load_or_create_fernet, seal
from circuless_node.models import AccessLog, AgreementCache, Resource, ServiceCredential, Tenant
from circuless_node.settings import Settings
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
UPSTREAM = "http://10.0.0.5:8080/api"


@pytest.fixture
def node(realm: FixtureRealm, tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        issuer=realm.issuer,
        database_url=f"sqlite:///{tmp_path / 'node.db'}",
        data_dir=tmp_path / "data",
        gateway_base_url="https://alpha.nodes.circuless.eu",
        cors_allow_origins=["https://ui.circuless.eu"],
    )


class Upstream:
    """Records what the node sent, and decides what comes back."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.respond = lambda request: httpx.Response(200, json={"ok": True})

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            self.seen.append(request)
            return self.respond(request)

        return httpx.MockTransport(handler)

    @property
    def last(self) -> httpx.Request:
        assert self.seen, "the node never called the upstream"
        return self.seen[-1]


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def app(node: Settings, upstream: Upstream):
    built = create_public_app(node)
    built.state.engine = migrated_engine(node)
    built.state.upstream_transport = upstream.transport()
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


def make_service(
    app,
    *,
    slug: str = "optimiser",
    endpoint_url: str = UPSTREAM,
    visibility: Visibility = Visibility.ORG,
    invoke_policy: dict | None = None,
    status: ResourceStatus = ResourceStatus.ACTIVE,
) -> uuid.UUID:
    tid = tenant_id(app)
    with Session(app.state.engine) as session, tenant_scope(session, tid):
        resource = Resource(
            tenant_id=tid,
            slug=slug,
            kind=ResourceKind.SERVICE,
            shape=Shape.SERVICE,
            title="Process optimiser",
            theme=Theme.PROCESSING,
            classification=Classification.NON_SENSITIVE,
            discoverability=Discoverability.HIDDEN,
            visibility=visibility,
            status=status,
            endpoint_url=endpoint_url,
            invoke_policy=invoke_policy,
        )
        session.add(resource)
        session.commit()
        session.refresh(resource)
        return resource.id


def set_credential(app, node: Settings, resource_id: uuid.UUID, **kwargs) -> None:
    tid = tenant_id(app)
    with Session(app.state.engine) as session, tenant_scope(session, tid):
        session.add(
            ServiceCredential(
                tenant_id=tid,
                resource_id=resource_id,
                secret=seal(load_or_create_fernet(node), kwargs.pop("plaintext")),
                set_by_sub="11111111-1111-1111-1111-111111111111",
                **kwargs,
            )
        )
        session.commit()


def url(resource_id: uuid.UUID, path: str = "run") -> str:
    return f"/v1/t/alpha/resources/{resource_id}/invoke/{path}"


def log_rows(app) -> list[AccessLog]:
    with Session(app.state.engine) as session, all_tenants(session):
        return list(session.exec(select(AccessLog).order_by(col(AccessLog.ts))).all())


# --- what the upstream receives (invariant 10) ----------------------------------------


def test_the_callers_credentials_never_reach_the_upstream(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    """`Authorization` is for *this* node. Handing it to a partner's service would let
    that service replay it here as the caller."""
    resource_id = make_service(app)
    client.post(
        url(resource_id),
        headers={**alpha_user, "Cookie": "session=abc", "Content-Type": "application/json"},
        content=b"{}",
    )

    sent = upstream.last.headers
    assert alpha_user["Authorization"] not in sent.values()
    assert "cookie" not in sent
    assert sent.get("content-type") == "application/json"


def test_an_inbound_circuless_header_cannot_be_forged(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    """The single most important strip. `X-CIRCULess-*` is how the node tells the
    upstream who is calling, and the upstream cannot tell the node's word from the
    caller's — so a consumer sending `X-CIRCULess-Org: someone-else` would simply be
    believed."""
    resource_id = make_service(app)
    client.post(
        url(resource_id),
        headers={
            **alpha_user,
            "X-CIRCULess-Org": "beta",
            "X-CIRCULess-Subject": "somebody-else",
            "X-CIRCULess-Actor": "forged",
        },
    )

    sent = upstream.last.headers
    assert sent["x-circuless-org"] == "alpha"
    assert sent["x-circuless-subject"] != "somebody-else"
    assert uuid.UUID(sent["x-circuless-subject"])
    assert sent["x-circuless-actor"] == "test-ui"


def test_the_node_says_who_is_calling(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    resource_id = make_service(app)
    response = client.post(url(resource_id), headers=alpha_user)

    sent = upstream.last.headers
    assert sent["x-circuless-resource"] == str(resource_id)
    assert sent["x-circuless-org"] == "alpha"
    assert sent["x-circuless-request-id"] == response.headers["X-CIRCULess-Request-Id"]


def test_only_the_allowlist_is_forwarded(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    """An allowlist that grows by exception is an allowlist; one that grows by sympathy
    is not."""
    resource_id = make_service(app)
    client.post(
        url(resource_id),
        headers={
            **alpha_user,
            "Accept": "application/json",
            "Idempotency-Key": "abc-123",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://somewhere.example/",
            "User-Agent": "curl/8",
        },
    )

    sent = upstream.last.headers
    assert sent["accept"] == "application/json"
    assert sent["idempotency-key"] == "abc-123"
    for dropped in ("x-requested-with", "referer"):
        assert dropped not in sent


def test_mcp_headers_only_reach_a_streaming_service(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    plain = make_service(app, slug="plain")
    streaming = make_service(app, slug="stream", invoke_policy={"streaming": True})
    mcp = {"Mcp-Session-Id": "s-1", "Last-Event-ID": "42"}

    client.post(url(plain), headers={**alpha_user, **mcp})
    assert "mcp-session-id" not in upstream.last.headers

    client.post(url(streaming), headers={**alpha_user, **mcp})
    assert upstream.last.headers["mcp-session-id"] == "s-1"
    assert upstream.last.headers["last-event-id"] == "42"


def test_the_upstream_credential_is_injected(
    client: TestClient, app, node: Settings, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    resource_id = make_service(app)
    set_credential(app, node, resource_id, scheme="bearer", plaintext="sk-upstream-9")

    client.post(url(resource_id), headers=alpha_user)
    assert upstream.last.headers["authorization"] == "Bearer sk-upstream-9"


def test_an_api_key_credential_uses_its_own_header(
    client: TestClient, app, node: Settings, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    resource_id = make_service(app)
    set_credential(
        app, node, resource_id, scheme="header", header_name="x-api-key", plaintext="k-1"
    )

    client.post(url(resource_id), headers=alpha_user)
    assert upstream.last.headers["x-api-key"] == "k-1"
    assert "authorization" not in upstream.last.headers


def test_the_path_and_query_reach_the_upstream(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    resource_id = make_service(app)
    client.post(url(resource_id, "jobs/7/run") + "?mode=fast", headers=alpha_user)

    assert str(upstream.last.url) == "http://10.0.0.5:8080/api/jobs/7/run?mode=fast"


# --- what comes back ---------------------------------------------------------------------


def test_the_response_body_and_type_come_through(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    upstream.respond = lambda r: httpx.Response(
        200, content=b'{"score":0.91}', headers={"Content-Type": "application/json"}
    )
    response = client.post(url(make_service(app)), headers=alpha_user)

    assert response.status_code == 200
    assert response.json() == {"score": 0.91}


def test_an_upstream_error_body_is_passed_through(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    """Invariant 15 allows this only here: a service's own error is the one thing the
    caller needs and the node cannot usefully restate."""
    upstream.respond = lambda r: httpx.Response(422, json={"error": "parameter out of range"})
    response = client.post(url(make_service(app)), headers=alpha_user)

    assert response.status_code == 422
    assert response.json() == {"error": "parameter out of range"}


def test_set_cookie_is_stripped(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    """A partner's service setting a cookie through the node would be setting it on the
    node's origin — a session-fixation and CSRF vector aimed at every *other* tenant's
    endpoints on the same host."""
    upstream.respond = lambda r: httpx.Response(
        200, headers={"Set-Cookie": "sid=attacker; Path=/", "Server": "nginx/1.25"}
    )
    response = client.post(url(make_service(app)), headers=alpha_user)

    assert "set-cookie" not in {k.lower() for k in response.headers}
    assert (
        "server" not in {k.lower() for k in response.headers}
        or response.headers.get("server") != "nginx/1.25"
    )


def test_a_202_passes_through_untouched(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    """The node holds no job state."""
    upstream.respond = lambda r: httpx.Response(
        202, headers={"Location": "http://10.0.0.5:8080/api/jobs/7", "Retry-After": "5"}
    )
    response = client.post(url(make_service(app)), headers=alpha_user, follow_redirects=False)

    assert response.status_code == 202
    assert response.headers["retry-after"] == "5"


def test_a_redirect_is_rewritten_back_through_the_node(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    """A `Location` the consumer followed directly would take them out from behind the
    node — past `decide()`, past the agreement and past the log."""
    resource_id = make_service(app)
    upstream.respond = lambda r: httpx.Response(
        303, headers={"Location": "http://10.0.0.5:8080/api/jobs/7?x=1"}
    )
    # `follow_redirects=False` or the client chases the rewritten Location — which is
    # itself a decent sign the rewrite worked, but makes the assertion about the wrong
    # response.
    response = client.post(url(resource_id), headers=alpha_user, follow_redirects=False)

    assert response.status_code == 303
    # The tenant **slug**, not its id: `/v1/t/{tenant_slug}/...` is what the router
    # matches. This assertion used to read `{tenant_id(app)}` and passed, because it was
    # written from the same mistaken code it was checking — the rewritten URL was a 404.
    # `test_reference_service.py` follows it for real, which is what found that.
    assert response.headers["location"] == (
        f"https://alpha.nodes.circuless.eu/v1/t/alpha/resources/{resource_id}/invoke/jobs/7?x=1"
    )


def test_a_node_with_no_gateway_url_still_rewrites(
    realm: FixtureRealm, tmp_path, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    """A node configured with neither a gateway URL nor an overlay address.

    Every other test here sets `gateway_base_url`, which short-circuited the other half
    of the `or` in `_invoke_base` — where a property was being called as a method. That
    threw on every redirect and every `202`, and no test reached it. The first node
    deployed without a gateway URL found it in about a minute.

    The `Location` is relative, which is right rather than a fallback: it resolves
    against whatever address the caller used to reach this node.
    """
    settings = Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        issuer=realm.issuer,
        database_url=f"sqlite:///{tmp_path / 'node.db'}",
        data_dir=tmp_path / "data",
        cors_allow_origins=["https://ui.circuless.eu"],
    )
    assert settings.gateway_base_url is None
    assert settings.effective_overlay_base_url is None

    built = create_public_app(settings)
    built.state.engine = migrated_engine(settings)
    built.state.upstream_transport = upstream.transport()
    with Session(built.state.engine) as session:
        session.add(Tenant(org_id=ALPHA_ORG, group_path="/orgs/alpha", slug="alpha"))
        session.commit()
    resource_id = make_service(built)

    upstream.respond = lambda r: httpx.Response(
        303, headers={"Location": "http://10.0.0.5:8080/api/jobs/7"}
    )
    with TestClient(built, raise_server_exceptions=False) as local:
        response = local.get(url(resource_id), headers=alpha_user, follow_redirects=False)

    assert response.status_code == 303, response.text
    assert response.headers["location"] == (f"/v1/t/alpha/resources/{resource_id}/invoke/jobs/7")


def test_a_redirect_elsewhere_is_refused(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    upstream.respond = lambda r: httpx.Response(
        302, headers={"Location": "https://evil.example/steal"}
    )
    response = client.post(url(make_service(app)), headers=alpha_user, follow_redirects=False)

    assert response.status_code == 502
    assert response.json()["reason"] == "upstream_error"


def test_the_node_does_not_follow_redirects_itself(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    """Following it here would mean the rewrite never ran."""
    upstream.respond = lambda r: httpx.Response(
        307, headers={"Location": "http://10.0.0.5:8080/api/elsewhere"}
    )
    client.post(url(make_service(app)), headers=alpha_user, follow_redirects=False)
    assert len(upstream.seen) == 1


# --- authorisation, the same as a download -----------------------------------------------


def test_an_agreement_permitting_invoke_is_what_gets_you_here(
    client: TestClient, app, beta_user: dict[str, str]
) -> None:
    resource_id = make_service(app, visibility=Visibility.AGREEMENT)
    assert client.post(url(resource_id), headers=beta_user).status_code == 403

    with Session(app.state.engine) as session:
        session.add(
            AgreementCache(
                id=uuid.uuid4(),
                provider_org="alpha",
                consumer_org="beta",
                resource_id=resource_id,
                actions="invoke",
                valid_from=datetime.now(UTC) - timedelta(days=1),
                status="accepted",
            )
        )
        session.commit()

    assert client.post(url(resource_id), headers=beta_user).status_code == 200


def test_a_read_only_agreement_does_not_permit_invoke(
    client: TestClient, app, beta_user: dict[str, str]
) -> None:
    resource_id = make_service(app, visibility=Visibility.AGREEMENT)
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

    assert client.post(url(resource_id), headers=beta_user).status_code == 403


def test_a_node_principal_may_not_invoke(client: TestClient, app, realm: FixtureRealm) -> None:
    resource_id = make_service(app, visibility=Visibility.PUBLIC)
    response = client.post(url(resource_id), headers=bearer(realm.node_token()))
    assert response.status_code == 403
    assert response.json()["reason"] == "node_principal_not_permitted"


def test_a_refused_call_never_reaches_the_upstream(
    client: TestClient, app, upstream: Upstream, beta_user: dict[str, str]
) -> None:
    resource_id = make_service(app, visibility=Visibility.ORG)
    assert client.post(url(resource_id), headers=beta_user).status_code == 403
    assert upstream.seen == [], "the node called the upstream for a refused request"


def test_the_call_is_logged_with_the_bytes_returned(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    upstream.respond = lambda r: httpx.Response(200, content=b"0123456789")
    resource_id = make_service(app)
    client.post(url(resource_id), headers=alpha_user)

    entry = log_rows(app)[-1]
    assert entry.action == "invoke"
    assert entry.decision == "allow"
    assert entry.resource_id == resource_id
    assert entry.bytes == 10


def test_a_withdrawn_service_cannot_be_invoked(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    resource_id = make_service(app, status=ResourceStatus.WITHDRAWN)
    assert client.post(url(resource_id), headers=alpha_user).status_code == 404
    assert upstream.seen == []


# --- the policy -----------------------------------------------------------------------------


def test_a_method_the_service_does_not_permit_is_refused(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    resource_id = make_service(app, invoke_policy={"idempotent_methods": ["GET"]})
    assert client.post(url(resource_id), headers=alpha_user).status_code == 405
    assert client.get(url(resource_id), headers=alpha_user).status_code == 200
    assert len(upstream.seen) == 1


def test_destructive_methods_are_not_permitted_by_default(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    """`GET` and `POST` cover nearly every service; changing a partner's state is
    something a provider should have to say."""
    resource_id = make_service(app)
    assert client.delete(url(resource_id), headers=alpha_user).status_code == 405
    assert client.put(url(resource_id), headers=alpha_user).status_code == 405
    assert client.post(url(resource_id), headers=alpha_user).status_code == 200


def test_a_request_over_the_limit_is_refused(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    resource_id = make_service(app, invoke_policy={"max_request_bytes": 16})
    response = client.post(url(resource_id), headers=alpha_user, content=b"x" * 64)

    assert response.status_code == 413
    assert upstream.seen == []


def test_a_timeout_is_a_gateway_timeout(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    upstream.respond = slow
    response = client.post(url(make_service(app)), headers=alpha_user)
    assert response.status_code == 504
    assert response.json()["reason"] == "upstream_timeout"


def test_an_unreachable_service_does_not_leak_its_address(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    """The exception carries the upstream's address, which is the provider's internal
    topology and not the consumer's business."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    upstream.respond = refuse
    response = client.post(url(make_service(app)), headers=alpha_user)

    assert response.status_code == 502
    assert "10.0.0.5" not in response.text


# --- the SSRF guard (the reason this component needed a question) ---------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8001",
        "http://localhost:8001/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0:8001",
        "http://[::1]:8001",
        "file:///etc/passwd",
    ],
)
def test_an_endpoint_pointing_at_this_host_is_refused_at_registration(
    client: TestClient, app, alpha_admin: dict[str, str], endpoint: str
) -> None:
    """The node runs its internal socket on loopback — `/internal/authz`, `/metrics`,
    the docs — which R8 requires to be unreachable through the gateway. Without this,
    a provider registers `http://127.0.0.1:8001` and any consumer with an agreement
    reads it from outside through `/invoke`."""
    response = client.post(
        "/v1/t/alpha/resources",
        headers=alpha_admin,
        json={
            "slug": "sneaky",
            "kind": "service",
            "title": "Sneaky",
            "theme": "processing",
            "classification": "non-sensitive",
            "endpoint_url": endpoint,
        },
    )
    assert response.status_code in (400, 422), response.text


def test_the_guard_runs_again_at_call_time(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    """A registration-time check alone is not enough: a hostname that resolved to a
    partner's server yesterday can resolve to 127.0.0.1 today. Simulated by writing the
    endpoint straight into the database, which is also what a compromised row looks
    like."""
    resource_id = make_service(app)
    tid = tenant_id(app)
    with Session(app.state.engine) as session, tenant_scope(session, tid):
        resource = session.get(Resource, resource_id)
        resource.endpoint_url = "http://127.0.0.1:8001/metrics"
        session.add(resource)
        session.commit()

    response = client.post(url(resource_id), headers=alpha_user)
    assert response.status_code == 422
    assert upstream.seen == []


def test_a_private_address_is_allowed(client: TestClient, app, alpha_admin: dict[str, str]) -> None:
    """Partner services live on private networks — D20 puts them there deliberately.
    The line is drawn at addresses that can only mean "this machine"."""
    response = client.post(
        "/v1/t/alpha/resources",
        headers=alpha_admin,
        json={
            "slug": "legit",
            "kind": "service",
            "title": "Legit",
            "theme": "processing",
            "classification": "non-sensitive",
            "endpoint_url": "http://optimiser.internal:8080",
        },
    )
    assert response.status_code == 201


def test_a_traversal_in_the_invoke_path_is_refused(
    client: TestClient, app, upstream: Upstream, alpha_user: dict[str, str]
) -> None:
    resource_id = make_service(app)
    response = client.post(url(resource_id, "%2e%2e%2f%2e%2e%2fadmin"), headers=alpha_user)

    assert response.status_code == 400
    assert response.json()["reason"] == "path_not_allowed"
    assert upstream.seen == []


# --- invoke_policy validation (N5) ------------------------------------------------------------


@pytest.mark.parametrize(
    "policy",
    [
        {"timeout_s": "banana"},
        {"timeout_s": -1},
        {"idempotent_methods": ["FLY"]},
        {"idempotent_methods": []},
        {"max_request_bytes": 0},
        {"unknown_field": 1},
    ],
)
def test_a_malformed_invoke_policy_is_refused_at_registration(
    client: TestClient, app, alpha_admin: dict[str, str], policy: dict
) -> None:
    """It was an opaque JSON column: the node accepted anything and the failure landed
    on a consumer's request weeks later, looking like the node's fault."""
    response = client.post(
        "/v1/t/alpha/resources",
        headers=alpha_admin,
        json={
            "slug": "bad-policy",
            "kind": "service",
            "title": "Bad policy",
            "theme": "processing",
            "classification": "non-sensitive",
            "endpoint_url": UPSTREAM,
            "invoke_policy": policy,
        },
    )
    assert response.status_code == 422, response.text
