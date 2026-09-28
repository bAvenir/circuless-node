"""A real Keycloak for the tests, and the tokens they need from it.

Never a mocked issuer: the node's whole job is deciding what a token means, and a fake one
would agree with whatever the node believed. See `tests/realm/CONTRACT.md` for what this
realm is standing in for.

Three principal types, three ways in:

  * **user** — the authorization-code flow with PKCE, driven end to end. H2 disables the
    password grant on every client, so this is the only way that survives, and it is the
    same flow the real UI uses.
  * **service** and **node** — `private_key_jwt` client credentials, which H2 does not
    touch.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
import subprocess
import time
import urllib.parse
import uuid

import httpx2 as httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

HERE = pathlib.Path(__file__).resolve().parent
TESTS_DIR = HERE.parent
REALM_SPEC = TESTS_DIR / "realm" / "test-realm.json"
COMPOSE_FILE = TESTS_DIR / "compose" / "docker-compose.yml"

KEYCLOAK_URL = os.environ.get("CIRCULESS_TEST_KEYCLOAK_URL", "http://127.0.0.1:8090")
ADMIN_USER = os.environ.get("CIRCULESS_TEST_KEYCLOAK_ADMIN", "admin")
ADMIN_PASSWORD = os.environ.get("CIRCULESS_TEST_KEYCLOAK_PASSWORD", "admin")
USER_PASSWORD = "Test-Pa55word!"
REDIRECT_URI = "http://localhost:9999/callback"


def strip_comments(obj):
    """Drop `_comment` and `// …` keys. JSON has no comments and an unannotated fixture is
    a fixture nobody can review."""
    if isinstance(obj, dict):
        return {
            k: strip_comments(v)
            for k, v in obj.items()
            if k != "_comment" and not k.startswith("//")
        }
    if isinstance(obj, list):
        return [strip_comments(v) for v in obj]
    return obj


# --------------------------------------------------------------------- bringing it up


def _is_up(url: str) -> bool:
    try:
        return httpx.get(f"{url}/realms/master", timeout=2.0).status_code == 200
    except Exception:
        return False


def ensure_keycloak(url: str = KEYCLOAK_URL, timeout: float = 180.0) -> str:
    """Use a Keycloak that is already running, or start one.

    Reusing matters: this suite grows for the rest of the project and gets run dozens of
    times a day, and a 30-second container start on every run is a tax on that. Leave the
    stack up and the inner loop stays fast; in CI, or from cold, it starts itself.
    """
    if _is_up(url):
        return url

    if url != KEYCLOAK_URL:
        raise RuntimeError(f"nothing is serving {url}, and it is not ours to start")

    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "--wait"],
        check=True,
        capture_output=True,
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_up(url):
            return url
        time.sleep(2)
    raise RuntimeError(f"Keycloak did not become ready at {url} within {timeout:.0f}s")


# ------------------------------------------------------------------------ admin client


class Admin:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self._client = httpx.Client(timeout=30.0)
        self.token = self._login()

    def _login(self) -> str:
        response = self._client.post(
            f"{self.base}/realms/master/protocol/openid-connect/token",
            data={
                "client_id": "admin-cli",
                "username": ADMIN_USER,
                "password": ADMIN_PASSWORD,
                "grant_type": "password",
            },
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def call(self, method: str, path: str, payload=None):
        response = self._client.request(
            method,
            f"{self.base}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {response.status_code} {response.text[:300]}")
        return response.json() if response.content else None

    def get(self, path: str):
        return self.call("GET", path)

    def post(self, path: str, payload=None):
        return self.call("POST", path, payload)

    def put(self, path: str, payload=None):
        return self.call("PUT", path, payload)

    def delete(self, path: str):
        return self.call("DELETE", path)


# ---------------------------------------------------------------------- building the realm


def _identity_mapper(claim: str, kind: str) -> dict:
    """Every mapper reads the SUBJECT. A hardcoded client claim would apply to every token
    that client obtains, which is R1."""
    shared = {
        "access.token.claim": "true",
        "id.token.claim": "false",
        "introspection.token.claim": "true",
    }
    if kind == "group-membership":
        return {
            "name": claim,
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            # Full path: the resolver keys on /orgs/<x>, so short names would break it.
            "config": {"claim.name": claim, "full.path": "true", **shared},
        }
    return {
        "name": claim,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-usermodel-attribute-mapper",
        "config": {
            "user.attribute": claim,
            "claim.name": claim,
            "jsonType.label": "String",
            "multivalued": "false",
            **shared,
        },
    }


class FixtureRealm:
    """Builds the fixture realm and hands out tokens."""

    def __init__(self, admin: Admin, spec: dict) -> None:
        self.admin = admin
        self.spec = spec
        self.name: str = spec["realm"]
        self.node_id: str = spec["nodeId"]
        self._keys: dict[str, rsa.RSAPrivateKey] = {}
        self._group_ids: dict[str, str] = {}

    @property
    def issuer(self) -> str:
        return f"{self.admin.base}/realms/{self.name}"

    # -- construction ---------------------------------------------------------------

    def rebuild(self) -> None:
        """Delete and recreate. A test realm has no state worth keeping, and starting from
        empty is the only way to be sure a run is not passing on yesterday's leftovers."""
        if any(r["realm"] == self.name for r in self.admin.get("/admin/realms")):
            self.admin.delete(f"/admin/realms/{self.name}")
        self.admin.post("/admin/realms", {"realm": self.name, "enabled": True})

        self._create_user_profile()
        self._create_groups()
        self._create_identity_scope()
        self._create_audience_scopes()
        self._create_clients()
        self._create_users()

    def _create_user_profile(self) -> None:
        # Keycloak 24+ drops attributes the profile does not declare, so principal_type and
        # friends would silently vanish from users without this.
        profile = self.admin.get(f"/admin/realms/{self.name}/users/profile")
        declared = {a["name"] for a in profile["attributes"]}
        for name in self.spec["userProfileAttributes"]:
            if name not in declared:
                profile["attributes"].append(
                    {
                        "name": name,
                        "displayName": name,
                        # Admin-only, exactly as in production: a user who could set their
                        # own org_id would choose their own organisation (T04).
                        "permissions": {"view": ["admin"], "edit": ["admin"]},
                        "multivalued": False,
                    }
                )
        self.admin.put(f"/admin/realms/{self.name}/users/profile", profile)

    def _create_groups(self) -> None:
        for path in self.spec["groups"]:
            parts = [p for p in path.split("/") if p]
            parent_id = None
            for depth, part in enumerate(parts):
                current = "/" + "/".join(parts[: depth + 1])
                if current in self._group_ids:
                    parent_id = self._group_ids[current]
                    continue
                endpoint = (
                    f"/admin/realms/{self.name}/groups/{parent_id}/children"
                    if parent_id
                    else f"/admin/realms/{self.name}/groups"
                )
                self.admin.post(endpoint, {"name": part})
                parent_id = self._group_id(current)
                self._group_ids[current] = parent_id

    def _group_id(self, path: str) -> str:
        parts = [p for p in path.split("/") if p]
        groups = self.admin.get(f"/admin/realms/{self.name}/groups")
        node = next(g for g in groups if g["name"] == parts[0])
        for part in parts[1:]:
            children = self.admin.get(f"/admin/realms/{self.name}/groups/{node['id']}/children")
            node = next(g for g in children if g["name"] == part)
        return node["id"]

    def _create_identity_scope(self) -> None:
        spec = self.spec["identityScope"]
        self.admin.post(
            f"/admin/realms/{self.name}/client-scopes",
            {
                "name": spec["name"],
                "protocol": "openid-connect",
                "attributes": {"include.in.token.scope": "false"},
                "protocolMappers": [
                    _identity_mapper(m["claim"], m["type"]) for m in spec["mappers"]
                ],
            },
        )

    def _create_audience_scopes(self) -> None:
        for spec in self.spec["audienceScopes"]:
            self.admin.post(
                f"/admin/realms/{self.name}/client-scopes",
                {
                    "name": spec["name"],
                    "protocol": "openid-connect",
                    "attributes": {"include.in.token.scope": "false"},
                    "protocolMappers": [
                        {
                            "name": "audience",
                            "protocol": "openid-connect",
                            "protocolMapper": "oidc-audience-mapper",
                            "config": {
                                "included.custom.audience": spec["audience"],
                                "access.token.claim": "true",
                                "id.token.claim": "false",
                            },
                        }
                    ],
                },
            )

    def _scope_ids(self) -> dict[str, str]:
        return {
            s["name"]: s["id"] for s in self.admin.get(f"/admin/realms/{self.name}/client-scopes")
        }

    def _create_clients(self) -> None:
        for spec in self.spec["clients"]:
            body = {
                "clientId": spec["clientId"],
                "enabled": True,
                "publicClient": spec["public"],
                "standardFlowEnabled": spec["public"],
                "directAccessGrantsEnabled": False,  # H2 disables it; the harness never needs it
                "implicitFlowEnabled": False,
                "serviceAccountsEnabled": not spec["public"],
            }
            if spec["public"]:
                body |= {
                    "redirectUris": spec["redirectUris"],
                    "attributes": {"pkce.code.challenge.method": "S256"},
                }
            else:
                body |= {
                    "clientAuthenticatorType": "client-jwt",
                    "attributes": {"use.jwks.url": "false"},
                }
            self.admin.post(f"/admin/realms/{self.name}/clients", body)

            uuid_ = self._client_uuid(spec["clientId"])
            self._assign_scopes(uuid_, spec["defaultScopes"], spec.get("optionalScopes", []))
            if not spec["public"]:
                self._keys[spec["clientId"]] = self._enroll(uuid_, spec["clientId"])
                self._set_service_account_attributes(uuid_, spec["serviceAccountAttributes"])

    def _client_uuid(self, client_id: str) -> str:
        return self.admin.get(f"/admin/realms/{self.name}/clients?clientId={client_id}")[0]["id"]

    def _assign_scopes(self, client_uuid: str, default: list[str], optional: list[str]) -> None:
        ids = self._scope_ids()
        # Removals before additions: a scope can only be attached one way, and a new client
        # arrives with the realm's defaults already in place.
        for kind, wanted in (("default", default), ("optional", optional)):
            path = f"/admin/realms/{self.name}/clients/{client_uuid}/{kind}-client-scopes"
            for scope in self.admin.get(path):
                if scope["name"] not in wanted:
                    self.admin.delete(f"{path}/{scope['id']}")
        for kind, wanted in (("default", default), ("optional", optional)):
            path = f"/admin/realms/{self.name}/clients/{client_uuid}/{kind}-client-scopes"
            have = {s["name"] for s in self.admin.get(path)}
            for name in wanted:
                if name not in have:
                    self.admin.put(f"{path}/{ids[name]}")

    def _enroll(self, client_uuid: str, client_id: str) -> rsa.RSAPrivateKey:
        """Register a self-signed X.509 certificate, as a node or service really does (D26)."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, client_id)])
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        client = self.admin.get(f"/admin/realms/{self.name}/clients/{client_uuid}")
        der = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
        self.admin.put(
            f"/admin/realms/{self.name}/clients/{client_uuid}",
            {
                **client,
                "attributes": {**client.get("attributes", {}), "jwt.credential.certificate": der},
            },
        )
        return key

    def _set_service_account_attributes(self, client_uuid: str, attributes: dict) -> None:
        user = self.admin.get(
            f"/admin/realms/{self.name}/clients/{client_uuid}/service-account-user"
        )
        self.admin.put(
            f"/admin/realms/{self.name}/users/{user['id']}",
            {**user, "attributes": {k: [v] for k, v in attributes.items()}},
        )

    def _create_users(self) -> None:
        for spec in self.spec["users"]:
            attributes = {}
            if spec["principal_type"] is not None:
                attributes["principal_type"] = [spec["principal_type"]]
            self.admin.post(
                f"/admin/realms/{self.name}/users",
                {
                    "username": spec["username"],
                    "enabled": True,
                    "emailVerified": True,
                    "email": f"{spec['username']}@example.invalid",
                    "firstName": spec["username"].split(".")[0].title(),
                    "lastName": "Test",
                    "attributes": attributes,
                    "credentials": [
                        {"type": "password", "value": USER_PASSWORD, "temporary": False}
                    ],
                },
            )
            user_id = self.admin.get(
                f"/admin/realms/{self.name}/users?username={spec['username']}&exact=true"
            )[0]["id"]
            for path in spec["groups"]:
                self.admin.put(
                    f"/admin/realms/{self.name}/users/{user_id}/groups/{self._group_id(path)}"
                )

    def register_certificate(self, client_id: str, certificate_pem: str) -> None:
        """Register a certificate generated elsewhere — by the node itself, in N17's tests.

        This is the manual enrollment step of §3.6: the node produces a certificate, an
        operator puts it on the client. Doing it this way round is the point; a harness
        that generated the key would be testing its own crypto rather than the node's.
        """
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode())
        der = base64.b64encode(certificate.public_bytes(serialization.Encoding.DER)).decode()
        client = self.admin.get(f"/admin/realms/{self.name}/clients?clientId={client_id}")[0]
        self.admin.put(
            f"/admin/realms/{self.name}/clients/{client['id']}",
            {
                **client,
                "attributes": {
                    **client.get("attributes", {}),
                    "jwt.credential.certificate": der,
                    "use.jwks.url": "false",
                },
            },
        )

    def user_id(self, username: str) -> str:
        """Keycloak's id for a fixture user — the `sub` its tokens carry."""
        found = self.admin.get(f"/admin/realms/{self.name}/users?username={username}&exact=true")
        if not found:
            raise KeyError(f"no fixture user {username!r}")
        return found[0]["id"]

    # -- tokens ---------------------------------------------------------------------

    def user_token(self, username: str, scope: str = "openid node:test-node") -> str:
        """Drive the authorization-code flow with PKCE, as the browser client does.

        Not the password grant: H2 turns that off on every client (SR-1.1.4), and a harness
        that depended on it would stop working the day that lands — with N2 and N3 built on
        top of it.
        """
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        authorize = f"{self.issuer}/protocol/openid-connect/auth?" + urllib.parse.urlencode(
            {
                "client_id": "test-ui",
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "scope": scope,
                "state": secrets.token_urlsafe(16),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )

        with httpx.Client(timeout=30.0, follow_redirects=False) as client:
            page = client.get(authorize)
            page.raise_for_status()

            # Keycloak's login page posts to a URL carrying the session and execution ids,
            # so it has to be read out of the form rather than constructed. Match the login
            # form by id: other forms appear on the page depending on the theme.
            match = re.search(
                r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page.text
            ) or re.search(r'action="([^"]+)"', page.text)
            if not match:
                raise RuntimeError("no login form on Keycloak's page — did the template change?")
            action = match.group(1).replace("&amp;", "&")

            # Keycloak marks KC_RESTART and KC_AUTH_SESSION_HASH `Secure; SameSite=None`,
            # even in dev mode over plain HTTP. A correct cookie jar will not send a Secure
            # cookie over http, so the login POST arrives without them and Keycloak answers
            # "Restart login cookie not found". Carrying them by hand keeps the tests on
            # http; the alternative is terminating TLS in front of the test container for
            # no benefit to what is being tested.
            cookies = "; ".join(
                header.split(";", 1)[0] for header in page.headers.get_list("set-cookie")
            )

            submitted = client.post(
                action,
                data={"username": username, "password": USER_PASSWORD},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": cookies,
                },
            )
            location = submitted.headers.get("location", "")
            if "code=" not in location:
                raise RuntimeError(f"login did not yield a code for {username!r} (scope={scope!r})")
            code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["code"][0]

            exchanged = client.post(
                f"{self.issuer}/protocol/openid-connect/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": "test-ui",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": verifier,
                },
            )
            exchanged.raise_for_status()
            return exchanged.json()["access_token"]

    def client_token(self, client_id: str, scope: str = "node:test-node") -> str:
        """Client credentials with private_key_jwt — how a service or a node authenticates."""
        key = self._keys[client_id]
        endpoint = f"{self.issuer}/protocol/openid-connect/token"
        now = dt.datetime.now(dt.UTC)
        assertion = jwt.encode(
            {
                "iss": client_id,
                "sub": client_id,
                # The assertion is audienced at the token endpoint, which is what stops it
                # being replayed against another issuer.
                "aud": endpoint,
                "jti": str(uuid.uuid4()),
                "iat": now,
                "exp": now + dt.timedelta(minutes=1),
            },
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode(),
            algorithm="RS256",
        )
        response = httpx.post(
            endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion,
                "scope": scope,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def service_token(self, scope: str = "node:test-node") -> str:
        return self.client_token("test-service", scope)

    def node_token(self, scope: str = "node:test-node") -> str:
        """A token the node must refuse with node_principal_not_permitted (D14)."""
        return self.client_token("test-node-principal", scope)


def claims_of(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


def load_spec() -> dict:
    return strip_comments(json.loads(REALM_SPEC.read_text()))
