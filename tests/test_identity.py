"""The node's own credentials (N17).

The node generates the keypair and an operator registers the certificate — that way
round, deliberately. A test that generated the key itself would be checking its own
crypto rather than the node's.

This covers M1's exit criterion: *the node obtains a `circuless-cloud` token with
`private_key_jwt` and an X.509-bound key*.
"""

from __future__ import annotations

import stat

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa

from circuless_node.identity import (
    CloudAuthenticationError,
    CloudCredentials,
    KeyPermissionsError,
    load_or_create_keypair,
)
from circuless_node.settings import Settings

from .harness.keycloak import FixtureRealm

# A client of its own. Enrollment replaces the registered certificate, and
# test-node-principal is shared by every test that needs a node token — swapping its
# certificate breaks all of them, which is how this was found.
NODE_CLIENT = "test-node-enrolling"


@pytest.fixture
def node_identity_settings(realm: FixtureRealm, tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        node_client_id=NODE_CLIENT,
        issuer=realm.issuer,
        database_url=f"sqlite:///{tmp_path / 'node.db'}",
        data_dir=tmp_path / "data",
    )


# ------------------------------------------------------------------- the keypair


def test_a_keypair_is_created_on_first_start(node_identity_settings: Settings) -> None:
    keypair = load_or_create_keypair(node_identity_settings)

    assert keypair.key_path.exists()
    assert keypair.certificate_path.exists()
    assert isinstance(keypair.private_key, rsa.RSAPrivateKey)
    assert keypair.private_key.key_size >= 2048


def test_the_private_key_is_owner_readable_only(node_identity_settings: Settings) -> None:
    """Invariant 17. A key anyone on the host can read is not a credential."""
    keypair = load_or_create_keypair(node_identity_settings)
    mode = stat.S_IMODE(keypair.key_path.stat().st_mode)

    assert mode == 0o600, f"private key is {mode:04o}"
    assert not mode & 0o077


def test_the_certificate_is_self_signed_and_names_the_node(
    node_identity_settings: Settings,
) -> None:
    """D26. It identifies a client, never an endpoint — no CIRCULess endpoint ever
    presents one of these (§3.5)."""
    certificate = load_or_create_keypair(node_identity_settings).certificate

    assert certificate.issuer == certificate.subject
    common_name = certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0]
    assert common_name.value == f"node:{node_identity_settings.node_id}"


def test_a_second_start_reuses_the_same_key(node_identity_settings: Settings) -> None:
    """Regenerating on restart would silently invalidate the registration an operator
    made, and the node would stop being able to reach the Cloud for no visible reason."""
    first = load_or_create_keypair(node_identity_settings)
    second = load_or_create_keypair(node_identity_settings)

    assert first.fingerprint == second.fingerprint
    assert first.private_key_pem() == second.private_key_pem()


def test_a_loosened_key_refuses_to_load(node_identity_settings: Settings) -> None:
    """Refused rather than quietly fixed.

    Silently chmod-ing it back hides that something loosened it — and on a shared host
    the key may already have been read, in which case carrying on is the wrong instinct.
    """
    keypair = load_or_create_keypair(node_identity_settings)
    keypair.key_path.chmod(0o644)

    with pytest.raises(KeyPermissionsError) as raised:
        load_or_create_keypair(node_identity_settings)

    assert "chmod 600" in str(raised.value), "the message should say how to fix it"


# ------------------------------------------------- authenticating to the Cloud API


def test_the_node_obtains_a_cloud_token(
    realm: FixtureRealm, node_identity_settings: Settings
) -> None:
    """M1's exit criterion, end to end against a real Keycloak."""
    keypair = load_or_create_keypair(node_identity_settings)
    # The manual enrollment step of §3.6: the node made the certificate, an operator
    # puts it on the client.
    realm.register_certificate(NODE_CLIENT, keypair.certificate_pem())

    token = CloudCredentials(node_identity_settings, keypair).token()
    claims = jwt.decode(token, options={"verify_signature": False})

    assert claims["aud"] == "circuless-cloud", "one audience per token, and the right one"
    assert claims["principal_type"] == "node"
    assert claims["node_id"] == realm.node_id
    assert claims["azp"] == NODE_CLIENT
    # Infrastructure, not an organisation — which is exactly why it cannot consume (D14).
    assert "org_id" not in claims
    assert claims.get("groups", []) == []


def test_that_token_is_refused_by_the_node_itself(
    realm: FixtureRealm, node_identity_settings: Settings, node_settings: Settings
) -> None:
    """The same credential that talks to the Cloud must get nowhere against a node.

    Worth asserting with the *real* node token rather than a synthesised one: this is the
    credential that actually exists on every deployed node.
    """
    from starlette.testclient import TestClient

    from circuless_node.app import create_public_app

    keypair = load_or_create_keypair(node_identity_settings)
    realm.register_certificate(NODE_CLIENT, keypair.certificate_pem())
    token = CloudCredentials(node_identity_settings, keypair).token()

    client = TestClient(create_public_app(node_settings), raise_server_exceptions=False)
    response = client.get("/v1/whoami", headers={"Authorization": f"Bearer {token}"})

    # Refused on audience: a Cloud-audienced token is not for this node. D14's
    # principal-type refusal is the backstop for a node token that *is* node-audienced.
    assert response.status_code == 401


def test_an_unregistered_certificate_is_refused_clearly(
    realm: FixtureRealm, node_identity_settings: Settings
) -> None:
    """The first thing that goes wrong on a real install: the operator has not registered
    the certificate yet. The message should say so rather than leaving someone reading
    Keycloak logs."""
    realm.register_certificate(NODE_CLIENT, _someone_elses_certificate())
    keypair = load_or_create_keypair(node_identity_settings)

    with pytest.raises(CloudAuthenticationError) as raised:
        CloudCredentials(node_identity_settings, keypair).token()

    assert "certificate registered" in str(raised.value)


def test_the_token_is_cached_between_calls(
    realm: FixtureRealm, node_identity_settings: Settings
) -> None:
    """Every heartbeat, catalogue push and sync pull needs one, and tokens live five
    minutes. Minting one per call turns a 30-second sync loop into a stream of
    authentications."""
    keypair = load_or_create_keypair(node_identity_settings)
    realm.register_certificate(NODE_CLIENT, keypair.certificate_pem())
    credentials = CloudCredentials(node_identity_settings, keypair)

    assert credentials.token() == credentials.token()


def test_invalidating_the_cache_fetches_a_fresh_token(
    realm: FixtureRealm, node_identity_settings: Settings
) -> None:
    """For a caller that got a 401 anyway — a key rotated on the Keycloak side, say."""
    keypair = load_or_create_keypair(node_identity_settings)
    realm.register_certificate(NODE_CLIENT, keypair.certificate_pem())
    credentials = CloudCredentials(node_identity_settings, keypair)

    first = credentials.token()
    credentials.invalidate()
    assert credentials.token() is not None
    assert jwt.decode(first, options={"verify_signature": False})["azp"] == NODE_CLIENT


def _someone_elses_certificate() -> str:
    """A valid certificate for a key the node does not hold."""
    import datetime as dt

    from cryptography.hazmat.primitives import hashes, serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "somebody-else")])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode()
