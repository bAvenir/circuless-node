"""Upstream service credentials (N10, invariant 11).

```
PUT    /v1/t/{tenant}/resources/{id}/credential    set or rotate
GET    /v1/t/{tenant}/resources/{id}/credential    whether one is set — never the value
DELETE /v1/t/{tenant}/resources/{id}/credential    remove it
```

This is the highest-value secret the node holds. Everything else it keeps is either its
own (the node keypair, which proves only that it is itself) or somebody's metadata. This
is a **partner's** credential to a **partner's** system, handed over so the node can call
it on a consumer's behalf, and the node's whole claim to be trustworthy infrastructure
rests on it never coming back out.

## Admins only, and never a service principal

N18's table, and the one row where a service principal is refused something it can
otherwise do. A pipeline account that can publish yesterday's run is useful; the same
account being able to rotate this would mean a compromised pipeline can point the node
at a server of its choosing — and send it this credential on the way.

## Never returned, by anything

`GET` answers *whether* a credential is set, of what kind, when, and by whom. Not the
value, not a prefix of it, not a length. It is decrypted in exactly one place — N9's
proxy, on its way into one outbound header — and `ServiceCredential.secret` holds a
Fernet token, so a database dump, a backup, or a stray log line is ciphertext.

The key is a file beside the node's private key, `0600`, and the node refuses to start
if that has loosened. The same guard as the node keypair, reused rather than rewritten:
one definition of "readable by its owner only" is one place to get it right.

**Key and ciphertext are separated by design.** The encrypted secrets live in the
database; the key lives on disk. A database backup on its own therefore discloses
nothing. A backup that sweeps up the node's directory *and* the database has both, which
is why the key backup belongs somewhere the data backup is not (G11).

## Typed, so that N9 builds the header

`bearer`, `header` or `basic` — not an arbitrary map of header names to values. The map
version is more flexible and would quietly undo invariant 10: an organisation's admin
could set `Host`, a hop-by-hop header, or an `X-CIRCULess-Subject` of their own choosing,
and the proxy would be forwarding a config field instead of asserting something it
derived. `render()` below is the only thing that turns a credential into a header, and
it constructs it.
"""

from __future__ import annotations

import base64
import re
import stat
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from pydantic import Field as PydanticField
from sqlmodel import Session

from .auth import require_subject
from .errors import NodeError, Reason
from .identity import KEY_FILE_MODE, KeyPermissionsError
from .management import ManagementAction
from .models import Resource, ServiceCredential
from .resources import authorised_tenant
from .settings import Settings
from .subject import Subject
from .tenancy import tenant_scope
from .vocabularies import ResourceKind, ResourceStatus

FERNET_KEY_FILENAME = "fernet.key"


class Scheme(StrEnum):
    """How the upstream expects to be authenticated."""

    #: `Authorization: Bearer <secret>`
    BEARER = "bearer"
    #: `<header_name>: <secret>` — API keys, which are at least as common as bearers.
    HEADER = "header"
    #: `Authorization: Basic base64(<secret>)`, where the secret is `user:password`.
    #: The username is inside the encrypted blob rather than beside it: half a
    #: credential in plaintext is still half a credential.
    BASIC = "basic"


# --- the header name allowlist ---------------------------------------------------------

#: RFC 7230 token characters. Anything else — a space, a colon, a CR or LF — cannot
#: appear in a header name, and refusing the whole grammar is what makes header
#: injection impossible rather than unlikely.
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

#: Names an upstream credential may never use. Hop-by-hop headers belong to the
#: connection and not to the message; `host` and `content-length` are the proxy's to
#: compute; and `x-circuless-*` is what the node asserts about *who is calling* (R5,
#: R6, R13) — a credential allowed to set one could forge the identity the node vouches
#: for, to the node's own upstream, with nothing in the request to show for it.
_REFUSED_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_REFUSED_PREFIX = "x-circuless-"


def check_header_name(name: str) -> str:
    """Validate a credential's header name, returning it lowercased."""
    if not _TOKEN.match(name):
        raise NodeError(
            422,
            Reason.INVALID_REQUEST,
            "a header name may contain only RFC 7230 token characters",
        )
    lowered = name.lower()
    if lowered in _REFUSED_HEADERS or lowered.startswith(_REFUSED_PREFIX):
        raise NodeError(
            422,
            Reason.INVALID_REQUEST,
            f"'{name}' is reserved: the node sets it, and a credential may not",
        )
    return lowered


# --- the key -----------------------------------------------------------------------------


def fernet_key_path(settings: Settings) -> Path:
    configured = settings.fernet_key_path
    if configured is not None:
        return configured.expanduser()
    return settings.data_dir.expanduser() / FERNET_KEY_FILENAME


def load_or_create_fernet(settings: Settings) -> Fernet:
    """The key that encrypts every upstream credential this node holds.

    Generated on first use, like the node keypair, so a fresh install has one before it
    has a credential to protect. Created at `0600` from the start rather than chmod-ed
    afterwards — there is no window in which it is readable, however short.
    """
    path = fernet_key_path(settings)
    if path.exists():
        _refuse_loose_permissions(path)
        return Fernet(path.read_bytes().strip())

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.touch(mode=KEY_FILE_MODE, exist_ok=False)
    path.write_bytes(key)
    return Fernet(key)


def _refuse_loose_permissions(path: Path) -> None:
    """Refuse to use a key anyone but its owner can read.

    The same rule as the node's private key and deliberately the same wording: fixing it
    silently would hide that something loosened it, and on a shared host every
    credential it protects should now be considered disclosed — which is a thing to
    tell someone, not to paper over.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise KeyPermissionsError(
            f"{path} has mode {mode:04o}; the node's credential key must be readable by "
            f"its owner only. Run: chmod 600 {path} — and treat every upstream "
            f"credential on this node as exposed, since anything here could have read it."
        )


# --- encryption ---------------------------------------------------------------------------


def seal(fernet: Fernet, plaintext: str) -> bytes:
    return fernet.encrypt(plaintext.encode())


def unseal(fernet: Fernet, sealed: bytes) -> str:
    try:
        return fernet.decrypt(sealed).decode()
    except InvalidToken:
        # The key changed, or the row did. Either way the node cannot call the upstream,
        # and saying so plainly is better than a 502 from a request made with nothing.
        raise NodeError(
            500,
            Reason.INTERNAL_ERROR,
            "this node cannot decrypt the stored credential; it must be set again",
        ) from None


def render(credential: ServiceCredential, plaintext: str) -> tuple[str, str]:
    """The one function that turns a credential into a header (N9 calls it).

    Returns `(name, value)`. Everything a scheme implies is decided here, so the proxy
    never has to know what a credential looks like — and adding a scheme is one branch
    in one place.
    """
    scheme = Scheme(credential.scheme)
    if scheme is Scheme.BEARER:
        return "authorization", f"Bearer {plaintext}"
    if scheme is Scheme.BASIC:
        return "authorization", "Basic " + base64.b64encode(plaintext.encode()).decode()
    return credential.header_name or "authorization", plaintext


# --- request shapes --------------------------------------------------------------------------


class CredentialIn(BaseModel):
    scheme: Scheme
    #: The token, the API key value, or — for `basic` — the password. Write-only: it
    #: appears in no response model anywhere.
    secret: str = PydanticField(min_length=1, max_length=4096)
    #: `basic` only.
    username: str | None = PydanticField(default=None, max_length=255)
    #: `header` only.
    header_name: str | None = PydanticField(default=None, max_length=64)


def credential_out(credential: ServiceCredential) -> dict:
    """Everything about a credential except the only part worth stealing."""
    return {
        "scheme": credential.scheme,
        "header_name": credential.header_name,
        "set_by": credential.set_by_sub,
        "created_at": credential.created_at.isoformat(),
        "updated_at": credential.updated_at.isoformat(),
    }


# --- the router -------------------------------------------------------------------------------


def credential_router() -> APIRouter:
    router = APIRouter()

    @router.put("/t/{tenant_slug}/resources/{resource_id}/credential")
    def set_credential(
        request: Request,
        tenant_slug: str,
        resource_id: uuid.UUID,
        body: CredentialIn,
        subject: Subject = Depends(require_subject),
    ) -> dict:
        """Set or rotate. Replaces whatever was there; there is no grace period.

        An upstream credential is rotated at the upstream, and the window in which both
        work is the upstream's business, not ours. Keeping the old one here "just in
        case" would mean holding a secret its owner believes they have revoked.
        """
        settings: Settings = request.app.state.settings
        if body.scheme is Scheme.HEADER:
            if not body.header_name:
                raise NodeError(422, Reason.INVALID_REQUEST, "scheme 'header' needs a header_name")
            header_name = check_header_name(body.header_name)
        else:
            header_name = None
            if body.header_name:
                raise NodeError(
                    422,
                    Reason.INVALID_REQUEST,
                    f"header_name belongs to scheme 'header', not '{body.scheme.value}'",
                )

        if body.scheme is Scheme.BASIC:
            if not body.username:
                raise NodeError(422, Reason.INVALID_REQUEST, "scheme 'basic' needs a username")
            if ":" in body.username:
                # A colon is the separator; allowing one would let a username carry a
                # password boundary of its own choosing.
                raise NodeError(422, Reason.INVALID_REQUEST, "a basic username may not contain ':'")
            plaintext = f"{body.username}:{body.secret}"
        else:
            if body.username:
                raise NodeError(
                    422,
                    Reason.INVALID_REQUEST,
                    f"username belongs to scheme 'basic', not '{body.scheme.value}'",
                )
            plaintext = body.secret

        with Session(request.app.state.engine) as session:
            tenant = authorised_tenant(
                request,
                session,
                subject,
                tenant_slug,
                ManagementAction.CREDENTIAL_SET,
                resource_id=resource_id,
            ).tenant
            with tenant_scope(session, tenant.id):
                resource = _service_or_refuse(session, resource_id)

                now = datetime.now(UTC)
                sealed = seal(load_or_create_fernet(settings), plaintext)
                existing = session.get(ServiceCredential, resource.id)
                if existing is None:
                    existing = ServiceCredential(
                        tenant_id=tenant.id,
                        resource_id=resource.id,
                        scheme=body.scheme.value,
                        header_name=header_name,
                        secret=sealed,
                        set_by_sub=subject.sub,
                        created_at=now,
                        updated_at=now,
                    )
                else:
                    existing.scheme = body.scheme.value
                    existing.header_name = header_name
                    existing.secret = sealed
                    existing.set_by_sub = subject.sub
                    existing.updated_at = now
                session.add(existing)
                session.commit()
                session.refresh(existing)
                return credential_out(existing)

    @router.get("/t/{tenant_slug}/resources/{resource_id}/credential")
    def read_credential(
        request: Request,
        tenant_slug: str,
        resource_id: uuid.UUID,
        subject: Subject = Depends(require_subject),
    ) -> dict:
        """Whether one is set, of what kind, and when. Never the value.

        Admins only, the same rule as setting it — reading the metadata is how you find
        out a credential exists to be attacked, and there is no one who needs that but
        cannot be trusted with the rest.
        """
        with Session(request.app.state.engine) as session:
            tenant = authorised_tenant(
                request,
                session,
                subject,
                tenant_slug,
                ManagementAction.CREDENTIAL_SET,
                resource_id=resource_id,
            ).tenant
            with tenant_scope(session, tenant.id):
                _service_or_refuse(session, resource_id)
                credential = session.get(ServiceCredential, resource_id)
                if credential is None:
                    raise NodeError(404, Reason.NOT_FOUND, "no credential is set")
                return credential_out(credential)

    @router.delete("/t/{tenant_slug}/resources/{resource_id}/credential", status_code=204)
    def remove_credential(
        request: Request,
        tenant_slug: str,
        resource_id: uuid.UUID,
        subject: Subject = Depends(require_subject),
    ) -> Response:
        with Session(request.app.state.engine) as session:
            tenant = authorised_tenant(
                request,
                session,
                subject,
                tenant_slug,
                ManagementAction.CREDENTIAL_SET,
                resource_id=resource_id,
            ).tenant
            with tenant_scope(session, tenant.id):
                credential = session.get(ServiceCredential, resource_id)
                if credential is None:
                    raise NodeError(404, Reason.NOT_FOUND, "no credential is set")
                session.delete(credential)
                session.commit()
        return Response(status_code=204)

    return router


def _service_or_refuse(session: Session, resource_id: uuid.UUID) -> Resource:
    resource = session.get(Resource, resource_id)
    if resource is None:
        raise NodeError(404, Reason.NOT_FOUND, "no such resource")
    if resource.status is not ResourceStatus.ACTIVE:
        raise NodeError(
            409,
            Reason.CONFLICT,
            "this resource is withdrawn and awaiting purge; it cannot be changed",
        )
    if resource.kind is not ResourceKind.SERVICE:
        raise NodeError(
            400, Reason.UNSUPPORTED, "only a service resource has an upstream to authenticate to"
        )
    return resource
