"""Token verification (N2).

The checks, in this order (CLAUDE.md invariant 3):

1. **signature** against the cached JWKS — unknown `kid` refetches once, throttled (R12);
2. **`iss`** — the configured issuer;
3. **`aud`** — *exactly* `node:{node_id}`;
4. **`exp` / `nbf`** — with 60 s leeway (G13);
5. **`principal_type`** — `node` is refused (D14).

Steps 1–4 answer "is this token real and meant for me", and fail with **401**. Step 5 asks
"is this principal allowed to speak here at all", and fails with **403** — the token is
genuine, the caller simply is not permitted (T02 and T03 draw the line in the same place).

Nothing here interprets the claims beyond that. Turning them into a `Subject` — orgs,
admin-of, acting org — is N3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Request

from .errors import NodeError, Reason
from .jwks import JwksCache, SigningKeyUnavailableError
from .settings import Settings

# On-prem nodes drift. G13: a node whose clock is a few seconds off would otherwise reject
# tokens that are perfectly valid, intermittently, in a way nobody enjoys diagnosing.
CLOCK_LEEWAY_SECONDS = 60

# Asymmetric only. Allowing an HMAC algorithm here is the classic JWT confusion attack:
# the public key everyone can read becomes the shared secret an attacker signs with.
ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"]


@dataclass(frozen=True)
class VerifiedToken:
    """A token that passed every check. Claims only — N3 resolves them into a Subject."""

    claims: dict[str, Any]

    @property
    def sub(self) -> str:
        return self.claims["sub"]

    @property
    def principal_type(self) -> str:
        return self.claims["principal_type"]

    @property
    def actor(self) -> str | None:
        """`azp` — the client the token was issued to.

        For a service acting on a user's behalf this is the only trace of the service:
        Keycloak's token exchange carries no `act` claim (§3.3, confirmed by the Q2 spike).
        """
        return self.claims.get("azp")

    @property
    def groups(self) -> list[str]:
        return list(self.claims.get("groups", []))

    @property
    def org_id(self) -> str | None:
        return self.claims.get("org_id")

    @property
    def node_id(self) -> str | None:
        return self.claims.get("node_id")


class TokenVerifier:
    def __init__(self, settings: Settings, jwks: JwksCache | None = None) -> None:
        self.settings = settings
        self.jwks = jwks or JwksCache(settings.issuer)

    def verify(self, token: str) -> VerifiedToken:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise self._unauthenticated("malformed token") from None

        kid = header.get("kid")
        if not kid:
            raise self._unauthenticated("token has no kid")

        try:
            key = self.jwks.key_for(kid)
        except SigningKeyUnavailableError:
            # Either a key we have never seen, or Keycloak is unreachable and our cache
            # predates the rotation. Both are "we cannot verify this", not "this is a lie".
            raise self._unauthenticated("unknown signing key") from None

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=ALLOWED_ALGORITHMS,
                issuer=self.settings.issuer,
                audience=self.settings.audience,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError:
            # One reason code for every failure mode. Telling a caller which check failed
            # tells an attacker which part of the forgery to fix next.
            raise self._unauthenticated("token rejected") from None

        self._require_sole_audience(claims)
        self._reject_node_principal(claims)
        return VerifiedToken(claims)

    def _require_sole_audience(self, claims: dict[str, Any]) -> None:
        """`aud` must be exactly this node's, and nothing else.

        PyJWT is satisfied when the expected audience appears *among* the audiences, which
        is right for OAuth in general and wrong here: a token audienced for two nodes is
        replayable between them, which is the whole reason each node has its own audience
        (§5.4). Q2's change to the realm — dropping Keycloak's audience-resolve mapper —
        is what makes this strictness possible without false rejections.
        """
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else list(audience or [])
        if audiences != [self.settings.audience]:
            raise self._unauthenticated("audience is not this node alone")

    def _reject_node_principal(self, claims: dict[str, Any]) -> None:
        """D14. A node authenticates as infrastructure and never consumes.

        A missing `principal_type` is refused rather than treated as `user`. Keycloak's
        User Profile has no attribute defaults, so the claim genuinely can be absent — and
        a node whose service-account attribute was never set would then be read as a user
        and sail through the very check meant to stop it. Fail closed; onboarding sets the
        attribute on every principal.
        """
        principal_type = claims.get("principal_type")
        if principal_type is None:
            raise self._unauthenticated("token carries no principal_type")

        if principal_type == "node":
            raise NodeError(
                403,
                Reason.NODE_PRINCIPAL_NOT_PERMITTED,
                "a node principal may not consume or manage resources",
            )

    @staticmethod
    def _unauthenticated(detail: str) -> NodeError:
        return NodeError(401, Reason.INVALID_TOKEN, detail)


def require_token(request: Request) -> VerifiedToken:
    """FastAPI dependency. Every route on the public app depends on it (D21).

    There is no anonymous variant and there should never be one — the route-auth test
    enumerates every route and asserts 401 without a token.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        raise NodeError(401, Reason.INVALID_TOKEN, "a bearer token is required")

    verifier: TokenVerifier = request.app.state.verifier
    return verifier.verify(credential.strip())
