"""How the node proves it is itself (N17).

A node authenticates to the Cloud API as **infrastructure**: it carries a `node_id` and no
organisation, and its tokens are refused at every consumption endpoint (D14). What it can
do is push its catalogue, pull agreements, and heartbeat.

It authenticates with **`private_key_jwt`** (D18) rather than a shared secret, so nothing
ever has to be sent to anyone: the keypair is generated here, on first start, and only the
public half — wrapped in a self-signed X.509 certificate (D26) — is registered with
Keycloak. There is no secret in a handover email, no secret in git, and no secret in the
realm export.

The certificate is self-signed on purpose and identifies a *client*, never an endpoint —
no CIRCULess endpoint ever presents one of these (§3.5). CA-issued certificates later are
a change to registration, not to architecture.

**Enrollment is manual in the beta.** The node writes its certificate next to its key; an
operator registers it against the node's Keycloak client. §3.6 describes the intended
`circuless-node enroll --token …` flow; documenting it now is what stops the manual steps
quietly becoming the process.

**Losing the private key** means generating a new one and re-registering it. Losing the
Fernet key (N10) means re-entering upstream credentials. Both are documented rather than
engineered around (G11), and both belong in a key backup kept separate from the data
backup.
"""

from __future__ import annotations

import datetime as dt
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .oidc import discover
from .settings import Settings

KEY_FILENAME = "node.key"
CERTIFICATE_FILENAME = "node.crt"

#: Private key and certificate. 0600 on the key; the certificate is public by nature but
#: kept tidy alongside it.
KEY_FILE_MODE = 0o600
CERTIFICATE_FILE_MODE = 0o644

CERTIFICATE_LIFETIME = dt.timedelta(days=825)
ASSERTION_LIFETIME = dt.timedelta(minutes=1)

#: Refresh this long before expiry, so a request never races the clock.
TOKEN_REFRESH_MARGIN = dt.timedelta(seconds=30)

CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class KeyPermissionsError(RuntimeError):
    """The private key is readable by more than its owner."""


class CloudAuthenticationError(RuntimeError):
    """Keycloak refused the node's credentials."""


@dataclass(frozen=True)
class NodeKeypair:
    private_key: rsa.RSAPrivateKey
    certificate: x509.Certificate
    key_path: Path
    certificate_path: Path

    @property
    def fingerprint(self) -> str:
        """SHA-256 of the certificate, for recognising which key is registered.

        Comparing this against what Keycloak holds answers "is my certificate the one the
        realm knows about?" without anyone touching the private key.
        """
        return self.certificate.fingerprint(hashes.SHA256()).hex()

    def certificate_pem(self) -> str:
        return self.certificate.public_bytes(serialization.Encoding.PEM).decode()

    def private_key_pem(self) -> str:
        return self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()


def load_or_create_keypair(settings: Settings) -> NodeKeypair:
    """Load this node's keypair, generating one on first start.

    The private key never leaves the host: generated here, written here, read here.
    """
    data_dir = settings.data_dir.expanduser()
    key_path = data_dir / KEY_FILENAME
    certificate_path = data_dir / CERTIFICATE_FILENAME

    if key_path.exists() and certificate_path.exists():
        _refuse_loose_permissions(key_path)
        private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise RuntimeError(f"{key_path} is not an RSA private key")
        return NodeKeypair(private_key, certificate, key_path, certificate_path)

    data_dir.mkdir(parents=True, exist_ok=True)
    private_key, certificate = _generate(settings.node_id)

    # Created with the right mode from the start rather than chmod-ed afterwards, so the
    # key is never briefly world-readable between write and fix.
    key_path.touch(mode=KEY_FILE_MODE, exist_ok=False)
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    certificate_path.chmod(CERTIFICATE_FILE_MODE)

    return NodeKeypair(private_key, certificate, key_path, certificate_path)


def _refuse_loose_permissions(key_path: Path) -> None:
    """Refuse to start on a key anyone but its owner can read.

    Fixing it silently would hide that something loosened it — and on a shared host the
    key may already have been read, in which case carrying on is the wrong instinct.
    """
    mode = stat.S_IMODE(key_path.stat().st_mode)
    if mode & 0o077:
        raise KeyPermissionsError(
            f"{key_path} has mode {mode:04o}; the node's private key must be readable by "
            f"its owner only. Run: chmod 600 {key_path} — and consider the key exposed, "
            f"since anything on this host could have read it."
        )


def _generate(node_id: str) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, f"node:{node_id}"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "CIRCULess"),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        # Self-signed: issuer is the subject. This identifies a client, never an endpoint.
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        # A little slack for clock drift between here and Keycloak, as N2 allows for on
        # the way in.
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + CERTIFICATE_LIFETIME)
        .sign(private_key, hashes.SHA256())
    )
    return private_key, certificate


class CloudCredentials:
    """The node's `circuless-cloud` token, obtained with `private_key_jwt` and cached.

    Cached because every heartbeat, catalogue push and sync pull needs one, and tokens
    live five minutes — minting one per call would turn a 30-second sync loop into a
    steady stream of authentications.
    """

    def __init__(
        self,
        settings: Settings,
        keypair: NodeKeypair,
        *,
        timeout: float = 10.0,
    ) -> None:
        self._settings = settings
        self._keypair = keypair
        self._timeout = timeout
        self._token: str | None = None
        self._expires_at: dt.datetime | None = None
        self._token_endpoint: str | None = None
        self._lock = threading.Lock()

    @property
    def client_id(self) -> str:
        return self._settings.node_client_id

    def token(self) -> str:
        with self._lock:
            if self._token is not None and self._still_fresh():
                return self._token
            self._token, self._expires_at = self._request_token()
            return self._token

    def _still_fresh(self) -> bool:
        if self._expires_at is None:
            return False
        return dt.datetime.now(dt.UTC) + TOKEN_REFRESH_MARGIN < self._expires_at

    def _request_token(self) -> tuple[str, dt.datetime]:
        endpoint = self._resolve_token_endpoint()
        requested_at = dt.datetime.now(dt.UTC)

        response = httpx.post(
            endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_assertion_type": CLIENT_ASSERTION_TYPE,
                "client_assertion": self._client_assertion(endpoint),
                # Asked for explicitly rather than granted as a client default. One
                # audience per token (§5.4) applies to node tokens too: if every node
                # token carried circuless-cloud automatically, one that also named a node
                # audience would carry two, and a node would refuse it on audience before
                # D14 could refuse it on principal type.
                "scope": "circuless-cloud",
            },
            timeout=self._timeout,
        )
        if response.status_code != 200:
            # No token body in the message: it would be empty anyway, and the error text
            # from Keycloak can echo the assertion back.
            raise CloudAuthenticationError(
                f"Keycloak refused {self.client_id}: HTTP {response.status_code}. "
                "Is this node's certificate registered on its client?"
            )

        payload = response.json()
        # `expires_in` is what the server says; trusting our own clock less than its
        # arithmetic keeps the margin honest under drift.
        lifetime = dt.timedelta(seconds=int(payload.get("expires_in", 300)))
        return payload["access_token"], requested_at + lifetime

    def _client_assertion(self, endpoint: str) -> str:
        now = dt.datetime.now(dt.UTC)
        return jwt.encode(
            {
                "iss": self.client_id,
                "sub": self.client_id,
                # Audienced at the token endpoint, which is what stops the assertion being
                # replayed against a different issuer.
                "aud": endpoint,
                "jti": str(uuid.uuid4()),
                "iat": now,
                "exp": now + ASSERTION_LIFETIME,
            },
            self._keypair.private_key_pem(),
            algorithm="RS256",
        )

    def _resolve_token_endpoint(self) -> str:
        if self._token_endpoint is None:
            self._token_endpoint = discover(self._settings.issuer, self._timeout)["token_endpoint"]
        return self._token_endpoint

    def invalidate(self) -> None:
        """Drop the cached token. For a caller that got a 401 anyway — a key rotated on
        the Keycloak side, say — so the next call fetches a fresh one."""
        with self._lock:
            self._token = None
            self._expires_at = None
