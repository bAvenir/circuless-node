"""Token verification against a real Keycloak (N2).

These are M1's exit criteria, and T02/T03 among them are security-critical: an S failure
blocks acceptance of M5. Every token here is minted by a real issuer — a mocked one would
agree with whatever the node believed.
"""

from __future__ import annotations

import datetime as dt

import jwt
import pytest
from starlette.testclient import TestClient

from circuless_node.app import create_public_app
from circuless_node.auth import TokenVerifier
from circuless_node.errors import NodeError, Reason
from circuless_node.jwks import JwksCache, SigningKeyUnavailableError
from circuless_node.settings import Settings

from .harness.keycloak import FixtureRealm


@pytest.fixture
def client(node_settings: Settings) -> TestClient:
    return TestClient(create_public_app(node_settings), raise_server_exceptions=False)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------- what must be accepted


def test_a_user_token_is_accepted(client: TestClient, realm: FixtureRealm) -> None:
    response = client.get("/v1/whoami", headers=bearer(realm.user_token("alpha.user")))

    assert response.status_code == 200
    body = response.json()
    assert body["principal_type"] == "user"
    assert body["org_ids"] == ["alpha"]
    assert body["actor"] == "test-ui"


def test_a_service_token_is_accepted_with_its_org(client: TestClient, realm: FixtureRealm) -> None:
    response = client.get("/v1/whoami", headers=bearer(realm.service_token()))

    assert response.status_code == 200
    body = response.json()
    assert body["principal_type"] == "service"
    assert body["org_ids"] == ["alpha"], "a service's organisation is an attribute, not a group"
    assert body["admin_of"] == [], "a service is never an org admin (N18)"


def test_a_node_audienced_token_carries_no_name_or_email(realm: FixtureRealm) -> None:
    """D31, NFR12. Nodes never receive names; the AccessLog stays pseudonymous."""
    claims = jwt.decode(realm.user_token("alpha.user"), options={"verify_signature": False})
    assert "name" not in claims
    assert "email" not in claims


# --------------------------------------------------------------- what must be refused


def test_no_token_at_all_is_refused(client: TestClient) -> None:
    response = client.get("/v1/whoami")
    assert response.status_code == 401
    assert response.json()["reason"] == Reason.INVALID_TOKEN


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "not-even-a-scheme"},
    ],
)
def test_a_malformed_authorization_header_is_refused(
    client: TestClient, header: dict[str, str]
) -> None:
    assert client.get("/v1/whoami", headers=header).status_code == 401


def test_a_node_token_is_refused(client: TestClient, realm: FixtureRealm) -> None:
    """T02, security-critical. D14: a node authenticates as infrastructure and never
    consumes. The token is genuine and correctly audienced — hence 403, not 401."""
    response = client.get("/v1/whoami", headers=bearer(realm.node_token()))

    assert response.status_code == 403
    assert response.json()["reason"] == Reason.NODE_PRINCIPAL_NOT_PERMITTED


def test_a_token_for_another_node_is_refused(client: TestClient, realm: FixtureRealm) -> None:
    """T03, security-critical. Perfectly valid, signed by the right issuer, simply meant
    for somewhere else."""
    other = realm.user_token("alpha.user", scope="openid node:other-node")
    response = client.get("/v1/whoami", headers=bearer(other))

    assert response.status_code == 401
    assert response.json()["reason"] == Reason.INVALID_TOKEN


def test_a_token_with_no_principal_type_is_refused(client: TestClient, realm: FixtureRealm) -> None:
    """Fail closed. Keycloak's User Profile cannot default an attribute, so the claim can
    genuinely be absent — and reading that as `user` would let a node principal whose
    attribute was never set pass the check meant to reject it."""
    token = realm.user_token("no.principal.type")
    response = client.get("/v1/whoami", headers=bearer(token))

    assert response.status_code == 401
    assert response.json()["reason"] == Reason.INVALID_TOKEN


def test_a_tampered_token_is_refused(client: TestClient, realm: FixtureRealm) -> None:
    token = realm.user_token("alpha.user")
    head, payload, signature = token.split(".")
    tampered = f"{head}.{payload}.{signature[:-4]}AAAA"

    assert client.get("/v1/whoami", headers=bearer(tampered)).status_code == 401


def test_a_token_signed_by_a_stranger_is_refused(client: TestClient, realm: FixtureRealm) -> None:
    """The claims are exactly right; only the signature is not Keycloak's. Without the
    algorithm allowlist this is also where an HMAC confusion attack would land."""
    real = jwt.decode(realm.user_token("alpha.user"), options={"verify_signature": False})
    # A full-length key, so the point of the test is the wrong signer and not a weak one.
    forged = jwt.encode(real, "x" * 64, algorithm="HS256", headers={"kid": "forged"})

    assert client.get("/v1/whoami", headers=bearer(forged)).status_code == 401


def test_an_expired_token_becomes_a_401(
    node_settings: Settings, realm: FixtureRealm, monkeypatch
) -> None:
    """What is worth testing here is our mapping, not PyJWT's clock.

    Shifting real time to age a token is fragile and slow, and it would mostly be asserting
    that PyJWT checks `exp`. What can actually go wrong on our side is letting the library
    exception escape as a 500 — so the expiry is simulated and the response shape checked.
    """
    verifier = TokenVerifier(node_settings)
    token = realm.user_token("alpha.user")

    def always_expired(*_args, **_kwargs):
        raise jwt.ExpiredSignatureError("Signature has expired")

    monkeypatch.setattr(jwt, "decode", always_expired)

    with pytest.raises(NodeError) as raised:
        verifier.verify(token)
    assert raised.value.status_code == 401
    assert raised.value.reason == Reason.INVALID_TOKEN


# ------------------------------------------------------------------------ the JWKS cache


def test_an_unknown_kid_triggers_exactly_one_refetch(realm: FixtureRealm) -> None:
    """R12. Keycloak rotates its signing key; a node that never refetches rejects every
    token issued afterwards, forever, until someone restarts it."""
    cache = JwksCache(realm.issuer, min_refetch_interval=0)
    cache.warm()

    fetches = 0
    original = cache._refetch

    def counting_refetch() -> None:
        nonlocal fetches
        fetches += 1
        original()

    cache._refetch = counting_refetch  # type: ignore[method-assign]

    with pytest.raises(SigningKeyUnavailableError):
        cache.key_for("a-kid-that-does-not-exist")

    assert fetches == 1, "one refetch per unknown kid, not a retry storm"


def test_refetching_is_throttled(realm: FixtureRealm) -> None:
    """An unknown `kid` is attacker-controlled. Without a throttle, a stream of tokens
    signed with invented key ids becomes a stream of requests to Keycloak."""
    cache = JwksCache(realm.issuer, min_refetch_interval=3600)
    cache.warm()

    fetches = 0
    original = cache._refetch

    def counting_refetch() -> None:
        nonlocal fetches
        fetches += 1
        original()

    cache._refetch = counting_refetch  # type: ignore[method-assign]

    for _ in range(5):
        with pytest.raises(SigningKeyUnavailableError):
            cache.key_for("still-not-a-real-kid")

    assert fetches == 0, "the warm fetch was recent, so none of these may refetch"


def test_known_keys_keep_working_when_keycloak_is_unreachable(realm: FixtureRealm) -> None:
    """F16: stale keys beat no keys. The node keeps deciding while the Cloud is down."""
    cache = JwksCache(realm.issuer, min_refetch_interval=0)
    cache.warm()
    known_kid = next(iter(cache._keys))

    cache._jwks_uri = "http://127.0.0.1:1/certs"  # nothing listens here
    cache._issuer = "http://127.0.0.1:1/realms/gone"

    assert cache.key_for(known_kid) is not None


def test_verification_still_works_from_a_stale_cache(
    realm: FixtureRealm, node_settings: Settings
) -> None:
    cache = JwksCache(realm.issuer, min_refetch_interval=0)
    cache.warm()
    verifier = TokenVerifier(node_settings, cache)
    token = realm.user_token("alpha.user")

    cache._jwks_uri = "http://127.0.0.1:1/certs"
    cache._issuer = "http://127.0.0.1:1/realms/gone"

    assert verifier.verify(token).principal_type == "user"


def test_clock_leeway_is_sixty_seconds() -> None:
    """G13. On-prem clocks drift, and intermittent rejection is miserable to diagnose."""
    from circuless_node.auth import CLOCK_LEEWAY_SECONDS

    assert CLOCK_LEEWAY_SECONDS == 60
    assert dt.timedelta(seconds=CLOCK_LEEWAY_SECONDS) == dt.timedelta(minutes=1)


def test_a_token_audienced_for_two_nodes_is_refused(
    client: TestClient, realm: FixtureRealm
) -> None:
    """§5.4: request one node per token.

    This is the case PyJWT alone would let through — it is satisfied when the expected
    audience appears *among* the audiences, which is right for OAuth generally and wrong
    here. A token naming this node and another is replayable between the two, which
    defeats the point of per-node audiences. Hence the explicit sole-audience check.
    """
    both = realm.user_token("alpha.user", scope="openid node:test-node node:other-node")
    assert set(jwt.decode(both, options={"verify_signature": False})["aud"]) == {
        "node:test-node",
        "node:other-node",
    }, "the fixture must really produce two audiences, or this proves nothing"

    response = client.get("/v1/whoami", headers=bearer(both))
    assert response.status_code == 401
    assert response.json()["reason"] == Reason.INVALID_TOKEN
