"""What a service resource permits, and where the node may be sent (N9).

Two things live here because both are provider-supplied, both decide what the node
*does* rather than what it serves, and both are validated when the provider types them
rather than when a consumer trips over them.

## `invoke_policy` is typed now

It was an opaque JSON column: a provider could register `{"timeout_s": "banana"}` and
the node accepted it, so the failure landed on a consumer's request weeks later and
looked like the node's fault. Refusing it at registration puts the error in front of the
person who can fix it.

## `endpoint_url` decides where the node connects

Which makes it the node's one server-side request forgery surface, and worth more care
than a URL field usually gets — see `check_upstream_url`.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from .errors import NodeError, Reason

#: Methods a service permits unless it says otherwise. `GET` and `POST` cover nearly
#: every real service; `PUT`, `PATCH` and `DELETE` are the ones that change a partner's
#: state, and a provider should have to say so.
DEFAULT_METHODS = ("GET", "POST", "HEAD", "OPTIONS")

ALL_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


class InvokePolicy(BaseModel):
    """How this node calls one upstream service."""

    model_config = ConfigDict(extra="forbid")

    #: Whole-request deadline for a non-streaming call.
    timeout_s: float = PydanticField(default=30.0, gt=0, le=600)
    #: The deadline while a stream is open. Longer by nature: an SSE connection that is
    #: idle is working, not stuck.
    stream_timeout_s: float = PydanticField(default=300.0, gt=0, le=3600)
    #: The upstream answers `202` and does the work elsewhere. The node passes that
    #: through and holds no job state (invariant 10).
    is_async: bool = PydanticField(default=False, alias="async")
    #: **The methods this service permits.** Anything else is refused before the node
    #: connects.
    #:
    #: The name comes from the design, where it reads as "the methods that are safe to
    #: repeat". The behaviour agreed for the beta is the stricter one — an allowlist of
    #: what may be called at all — so the field is narrower than its name suggests and
    #: the node never retries anything. Worth renaming in the design; not worth renaming
    #: unilaterally here.
    idempotent_methods: list[str] = PydanticField(default=list(DEFAULT_METHODS))
    #: Largest request body the node will relay.
    max_request_bytes: int = PydanticField(default=10 * 1024 * 1024, gt=0)
    #: Server-sent events: the response is relayed unbuffered and the MCP session
    #: headers are forwarded.
    streaming: bool = False

    def permits(self, method: str) -> bool:
        return method.upper() in {entry.upper() for entry in self.idempotent_methods}

    @property
    def deadline(self) -> float:
        return self.stream_timeout_s if self.streaming else self.timeout_s


def parse_invoke_policy(raw: dict | None) -> InvokePolicy:
    """The stored policy, or the defaults. Raises `NodeError` on a malformed one."""
    if raw is None:
        return InvokePolicy()
    try:
        policy = InvokePolicy.model_validate(raw)
    except ValueError as invalid:
        raise NodeError(
            422, Reason.INVALID_REQUEST, f"invoke_policy is not valid: {invalid.errors()[0]['msg']}"
        ) from None

    unknown = {m.upper() for m in policy.idempotent_methods} - ALL_METHODS
    if unknown:
        raise NodeError(
            422,
            Reason.INVALID_REQUEST,
            f"unknown HTTP method(s) in idempotent_methods: {', '.join(sorted(unknown))}",
        )
    if not policy.idempotent_methods:
        raise NodeError(
            422,
            Reason.INVALID_REQUEST,
            "idempotent_methods may not be empty; the service would permit nothing",
        )
    return policy


# --- where the node may be sent ---------------------------------------------------------


class UpstreamNotAllowedError(NodeError):
    """The endpoint names somewhere the node must not connect to."""

    def __init__(self, detail: str) -> None:
        super().__init__(422, Reason.INVALID_REQUEST, detail)


def check_upstream_url(url: str, *, resolve: bool = False) -> None:
    """Refuse an endpoint that points back at this host or at link-local space.

    **This is the node's server-side request forgery check, and it is not decoration.**
    `endpoint_url` is provider-supplied and N9 makes the node connect to it. The node
    runs a second server on loopback — `/internal/authz`, `/metrics`, the API docs —
    which R8 requires to be unreachable through the gateway. Without this, a provider
    admin registers `http://127.0.0.1:8001`, and any consumer with an agreement reads
    the node's internal socket from the outside through `/invoke`. Link-local
    (`169.254.0.0/16`) is the cloud metadata service, which on a Hetzner host hands out
    credentials.

    **Private address space is allowed and must stay allowed.** `http://optimiser.
    internal:8080` on a partner's own network is the ordinary case, and D20 puts
    services on private container networks deliberately. The line is drawn at addresses
    that can only ever mean "this machine" or "this machine's hypervisor".

    Called twice, with and without `resolve`:

    * at registration (`resolve=False`), on the literal text, so the provider is told at
      the moment they typed it, without registration depending on DNS or on the service
      existing yet;
    * at call time (`resolve=True`), on every address the hostname resolves to, because
      a name that pointed at a partner's server on Tuesday can point at `127.0.0.1` on
      Wednesday and only this check sees that.

    **Residual, stated rather than hidden:** between resolving here and connecting a
    moment later, DNS can change again. Closing that needs the resolved address pinned
    into the connection while keeping the hostname for TLS, which is more machinery than
    the beta earns — the exposure is a narrow window against an attacker who already
    controls a provider admin account and the DNS for its endpoint.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UpstreamNotAllowedError("an endpoint_url must be http or https")
    host = parsed.hostname
    if not host:
        raise UpstreamNotAllowedError("an endpoint_url must name a host")

    if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(
        ".localhost"
    ):
        raise UpstreamNotAllowedError(
            "an endpoint_url may not point at this host; the node's internal socket "
            "lives there (R8)"
        )

    literal = _as_ip(host)
    if literal is not None:
        _refuse_reserved(literal, host)
        return

    if not resolve:
        return

    try:
        resolved = socket.getaddrinfo(host, parsed.port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise NodeError(
            502, Reason.UPSTREAM_ERROR, "the upstream address could not be resolved"
        ) from None
    for *_, sockaddr in resolved:
        _refuse_reserved(ipaddress.ip_address(sockaddr[0]), host)


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _refuse_reserved(address: ipaddress.IPv4Address | ipaddress.IPv6Address, host: str) -> None:
    # `is_unspecified` is 0.0.0.0 and ::, which route to this machine on Linux — the
    # same hole as loopback, reached by a different spelling.
    if address.is_loopback or address.is_unspecified:
        raise UpstreamNotAllowedError(
            f"'{host}' resolves to this host ({address}); the node's internal socket "
            "lives there (R8)"
        )
    if address.is_link_local:
        raise UpstreamNotAllowedError(
            f"'{host}' resolves to link-local space ({address}), which is where cloud "
            "metadata services hand out credentials"
        )
