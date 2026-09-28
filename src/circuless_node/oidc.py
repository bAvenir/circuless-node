"""Reading the realm's own description of itself.

Both the JWKS cache (N2) and the node's Cloud credentials (N17) need endpoints from the
issuer. Discovery rather than hardcoded Keycloak paths: the issuer is configuration, and
this is the one call that confirms the realm is the one we think it is.
"""

from __future__ import annotations

import httpx

DISCOVERY_PATH = "/.well-known/openid-configuration"


def discover(issuer: str, timeout: float = 5.0) -> dict:
    with httpx.Client(timeout=timeout) as client:
        response = client.get(f"{issuer.rstrip('/')}{DISCOVERY_PATH}")
        response.raise_for_status()
        return response.json()
