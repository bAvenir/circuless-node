"""The node's copy of Keycloak's signing keys.

Verification happens offline: the node never calls the Cloud on the request path (§3.7).
That only works if the keys are here already, which makes the refresh policy the whole of
this module.

**Refetch once on an unknown `kid`** (R12). Keycloak rotates its signing key, and a node
holding only the old one would reject every token issued afterwards — forever, until
someone restarted it. A cache that never refreshes is worse than no cache.

**Throttled**, because an unknown `kid` is attacker-controlled: a stream of tokens signed
with invented key ids would otherwise turn into a stream of requests to Keycloak.

**Stale keys beat no keys** (F16). If Keycloak is unreachable, the node keeps verifying
with what it has rather than failing closed. That window is bounded anyway: §3.7 notes
that an outage longer than the 5-minute token lifetime stops consumption regardless,
because nobody can obtain a fresh token. For the same reason the cache is in memory only —
persisting it across restarts would buy at most those five minutes.
"""

from __future__ import annotations

import threading
import time

import httpx
import jwt

from .oidc import discover


class SigningKeyUnavailableError(Exception):
    """No key for this `kid`, and refetching did not produce one."""


class JwksCache:
    def __init__(
        self,
        issuer: str,
        *,
        min_refetch_interval: float = 60.0,
        timeout: float = 5.0,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._min_refetch_interval = min_refetch_interval
        self._timeout = timeout

        self._keys: dict[str, jwt.PyJWK] = {}
        self._jwks_uri: str | None = None
        self._last_fetch: float | None = None
        # Verification runs in FastAPI's threadpool, so several requests can arrive at an
        # unknown kid at once. The lock keeps that to one refetch rather than N.
        self._lock = threading.Lock()

    @property
    def fetched(self) -> bool:
        return self._last_fetch is not None

    def key_for(self, kid: str) -> jwt.PyJWK:
        key = self._keys.get(kid)
        if key is not None:
            return key

        with self._lock:
            # Another thread may have refetched while this one waited.
            key = self._keys.get(kid)
            if key is not None:
                return key

            if self._may_refetch():
                self._refetch()
                key = self._keys.get(kid)
                if key is not None:
                    return key

        raise SigningKeyUnavailableError(f"no signing key for kid {kid!r}")

    def _may_refetch(self) -> bool:
        if self._last_fetch is None:
            return True
        return (time.monotonic() - self._last_fetch) >= self._min_refetch_interval

    def _refetch(self) -> None:
        """Replace the cached keys. On failure, keep the ones we have (F16)."""
        try:
            uri = self._resolve_jwks_uri()
            with httpx.Client(timeout=self._timeout) as client:
                document = client.get(uri).raise_for_status().json()
            self._keys = {k.key_id: k for k in jwt.PyJWKSet.from_dict(document).keys if k.key_id}
        except Exception:
            # Deliberately swallowed. An unreachable Keycloak must not turn into a 500 for
            # a caller holding a perfectly good token signed by a key we already hold.
            pass
        finally:
            # Recorded even on failure, so a persistently unreachable Keycloak is retried
            # on the throttle interval rather than on every request.
            self._last_fetch = time.monotonic()

    def _resolve_jwks_uri(self) -> str:
        if self._jwks_uri:
            return self._jwks_uri
        self._jwks_uri = discover(self._issuer, self._timeout)["jwks_uri"]
        return self._jwks_uri

    def warm(self) -> None:
        """Fetch ahead of the first request. Best-effort: a node must start even if
        Keycloak is down, and it will fetch on demand once one is reachable."""
        with self._lock:
            if not self.fetched:
                self._refetch()
