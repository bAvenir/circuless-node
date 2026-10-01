"""`/.well-known/circuless-node` (N12, F2, F15).

What a node says about itself to another participant: who it is, how to reach it, and
what it can be asked to do.

**It needs a token.** D21 has no anonymous routes in the beta, this one included — which
is unusual for a `.well-known` path and deliberate. An unauthenticated description of a
node is a free inventory for anyone scanning: its version, its overlay address, and
confirmation that a CIRCULess node is running here at all. The readers who need it are
platform participants, and they all have tokens.

It is one of the three paths allowed outside `/v1` (invariant 1). The prefix is for the
node's own API; `.well-known` is an interoperability convention and versioning it would
mean nobody could find it by convention.

## Two endpoints, and clients try the overlay first

A client that can join the CIRCULess overlay reaches the node directly and the Cloud is
not on the path at all (§5.6). The gateway is the fallback for browsers and for anything
that cannot install an agent. Advertising both, in preference order, is what lets a
client make that choice without being configured for it.

## What is deliberately absent

**The organisations this node hosts.** Anyone holding a token for this node could
otherwise learn who is on it. The Cloud keeps that mapping in its node registry and
restricts it to `platform-admin`; publishing it here to every authenticated caller would
route round that restriction. Who a partner shares infrastructure with is their business,
and a consumer never needs it — they find resources through the catalogue, which names
the organisation and not the node.

**Anything about a specific resource.** That is the catalogue's job, and it is filtered
by discoverability. This document is about the node.
"""

from __future__ import annotations

from typing import Any

from . import __version__
from .settings import Settings
from .vocabularies import Shape

#: The wire contract between nodes and clients. Bumped when the shape of what a node
#: serves changes in a way a client has to know about — not when the node is released.
#: A client checks this; `version` below is for the humans reading an incident.
PROTOCOL = "circuless/1"


def node_document(settings: Settings, *, version: str = __version__) -> dict[str, Any]:
    """What this node publishes about itself. Pure — settings in, document out."""
    return {
        "protocol": PROTOCOL,
        "node_id": settings.node_id,
        #: The audience a caller must hold to talk to this node at all. Published so a
        #: client can request the right scope rather than discovering the requirement
        #: through a 401 it cannot interpret.
        "audience": settings.audience,
        "issuer": settings.issuer,
        # The build, not the protocol. Genuinely useful during a federation incident —
        # "which nodes are still on 0.2.0" is a question someone will ask — and it is
        # also the most useful string for deciding which known issue to try, which is
        # why this route requires a token and why the Cloud's heartbeat carries the same
        # value for platform-admins who need it without one.
        "version": version,
        "endpoints": _endpoints(settings),
        "capabilities": {
            # What `decide()` can be asked to permit (N6). A client that sees no
            # `invoke` knows not to attempt a service call.
            "actions": ["read", "invoke"],
            "shapes": [shape.value for shape in Shape],
            "streaming": True,
            # D22. A provider choosing where to register needs to know this before they
            # try, and the refusal is a property of the node rather than of their data.
            "accepts_sensitive": not settings.refuses_sensitive,
            # No `max_upload_bytes` yet: N19 brings uploads and the limit that goes with
            # them. Advertising a limit nothing enforces is worse than advertising none.
        },
    }


def _endpoints(settings: Settings) -> list[dict[str, str]]:
    """Reachable addresses, most preferred first.

    A list rather than two named fields, so a client iterates in order and does not have
    to know which kinds exist. Adding a third — a relay, say — then costs a client
    nothing.

    An unconfigured address is omitted rather than sent as null: a client should try what
    is there, and "the operator has not set this" is not a thing to make every caller
    handle.
    """
    candidates = (
        # Detected from the interface unless overridden, so the published address is one
        # the node is actually on rather than one somebody typed.
        ("overlay", settings.effective_overlay_base_url),
        ("gateway", settings.gateway_base_url),
    )
    return [{"kind": kind, "url": url} for kind, url in candidates if url]
