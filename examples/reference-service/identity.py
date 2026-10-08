"""Tier 1: the same service, now aware of who is calling.

    uvicorn identity:app --port 8080

Everything below is the **entire cost** of making a service identity-aware. `service.py`
is untouched — this imports it and adds to it, so the diff between running
`service:app` and `identity:app` is this file and nothing else.

## What you get

`X-CIRCULess-Subject` — a stable, pseudonymous id for the person. Key your own per-user
data and your own roles on it. It is **not** a name or an email and never will be: node
tokens carry neither (D31), so there is nothing for the node to pass on even if it
wanted to.

`X-CIRCULess-Org` — the organisation they acted as. A person may belong to several; this
is the one that permitted *this* call.

`X-CIRCULess-Request-Id` — the node's id for this request, also in its access log. Put
it in yours. During an incident it is the only string that joins the two.

## What you take on

Those headers are the node's word, and they are trustworthy **because** the request
carried the key. A tier-0 service ignores them, so forging them achieves nothing. A
tier-1 service acts on them, so anyone who obtains the key can also claim to be anyone.

That raises the value of the key, and it is why a tier-1 service should also be
unreachable except from the node — not instead of the key, but behind it.

## What you must not do

Do not authenticate the user yourself. Keycloak issued the token, the node verified it,
the node checked the agreement and the node logged the call. The user's token never
arrives — the node strips `Authorization` so this service can never replay it. A service
that tries to re-establish the user's identity is reimplementing a decision that has
already been made, with less information.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, Header, Request
from service import Refused, app, require_key

log = logging.getLogger("csv-service")


@dataclass(frozen=True)
class Caller:
    """Who the node says is calling. Never what the caller said about themselves."""

    subject: str
    org: str | None
    resource: str | None
    request_id: str | None
    actor: str | None


def caller(
    _key: None = Depends(require_key),
    x_circuless_subject: str = Header(default=""),
    x_circuless_org: str = Header(default=""),
    x_circuless_resource: str = Header(default=""),
    x_circuless_request_id: str = Header(default=""),
    x_circuless_actor: str = Header(default=""),
) -> Caller:
    """Authenticate the node, then trust what it says. In that order.

    `require_key` runs first, as a dependency of this one — the headers below mean
    nothing without it.

    The second check is the one people leave out: a request carrying the key but no
    `X-CIRCULess-Subject` did not come through a node. It came from something holding
    the key, which is worth refusing rather than serving on behalf of nobody.
    """
    if not x_circuless_subject:
        raise Refused(401, "no X-CIRCULess-Subject: this request did not arrive through a node")

    log.info(
        "request_id=%s subject=%s org=%s",
        x_circuless_request_id or "-",
        x_circuless_subject,
        x_circuless_org or "-",
    )
    return Caller(
        subject=x_circuless_subject,
        org=x_circuless_org or None,
        resource=x_circuless_resource or None,
        request_id=x_circuless_request_id or None,
        # `azp`: the client the token was issued to. If a service called on the user's
        # behalf through token exchange, this is the only trace of it — an exchanged
        # token carries no `act` claim (Q2).
        actor=x_circuless_actor or None,
    )


@app.get("/whoami")
async def whoami(request: Request, who: Caller = Depends(caller)) -> dict:
    """Echo what arrived, so the contract can be inspected rather than trusted.

    Useful while integrating: it shows which headers the node sends, that a consumer's
    forged ones never arrive, and that the MCP session headers appear only when the
    resource is registered `streaming: true`.
    """
    return {
        "subject": who.subject,
        "org": who.org,
        "resource": who.resource,
        "request_id": who.request_id,
        "actor": who.actor,
        "all_headers": sorted(name.lower() for name in request.headers),
    }
