"""Finding this node's address on the CIRCULess overlay (N15).

NetBird allocates peer addresses from the **carrier-grade NAT range**, `100.64.0.0/10`
(RFC 6598). So "am I on the overlay, and at what address" is answerable by looking at
the local interfaces, with no NetBird binary, no API token and no configuration.

That matters because of how the node is deployed: the agent runs in a **separate
container** whose network namespace this one shares (see `deploy/README.md`). The
`netbird` CLI is therefore not in this container and shelling out to it is not an
option — but the interface it created is right here, because that is the whole point of
sharing the namespace.

## Why detect rather than configure

`/.well-known/circuless-node` publishes this address, and the failure worth preventing is
a node advertising an address it is not actually on. A setting is a line in a partner's
`.env` that nobody validates until a client cannot connect; an interface is the truth.

`CIRCULESS_NODE_OVERLAY_BASE_URL` remains as an override, for the cases detection cannot
know about — a node reached through a relay, or one whose address a gateway rewrites.

## What this deliberately does not do

**It does not prove reachability.** An address on an interface means the agent created
one, not that any peer can reach this node — that depends on the access rules, which live
on the NetBird server and are none of the node's business. `check` reports what was
found; whether a peer can use it is a question for whoever holds the console.
"""

from __future__ import annotations

import fcntl
import ipaddress
import socket
import struct

#: RFC 6598. NetBird's default pool, and not a range that appears on an ordinary LAN —
#: which is what makes the detection unambiguous rather than a guess.
OVERLAY_NETWORK = ipaddress.ip_network("100.64.0.0/10")

#: `SIOCGIFADDR` — ask the kernel for an interface's IPv4 address. Linux's value;
#: other platforms differ, and the ioctl simply fails there, which is handled.
SIOCGIFADDR = 0x8915


def detect_overlay_address() -> str | None:
    """This node's overlay address, or None if it is not on the overlay.

    None is an ordinary answer, not an error: a node runs perfectly well without the
    overlay — it reaches Keycloak and the Cloud API over the public internet, and only
    inbound traffic needs a peer. A development node usually has no overlay at all.
    """
    for address in _local_addresses():
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed in OVERLAY_NETWORK:
            return address
    return None


def overlay_base_url(port: int) -> str | None:
    """The detected address as a URL the `/.well-known` document can publish.

    **`http`, not `https`.** The node terminates no TLS on either socket, and on the
    overlay it does not need to: D28 says every hop is encrypted "with TLS on every
    publicly reachable endpoint, and WireGuard on the overlay", and §3 adds that
    overlay-only endpoints rely on WireGuard, with per-node certificates as the
    final-version path. The gateway endpoint stays `https` because Traefik terminates
    TLS where the public internet can reach it.

    This said `https` until the Q3 dry run, which settled it by measurement rather than
    by reading: a request to `http://100.98.65.67:8000/v1/whoami` over the overlay
    returned 200, while the document was advertising the same endpoint as `https`. A
    client following §5.6's "try overlay first" would have attempted TLS against a plain
    socket and fallen back to the gateway — if there was one — or simply failed.
    """
    address = detect_overlay_address()
    return f"http://{address}:{port}" if address else None


def _local_addresses() -> list[str]:
    """Every IPv4 address on this host's interfaces.

    `ioctl(SIOCGIFADDR)` on each interface from `socket.if_nameindex()`.

    Not `getaddrinfo(interface_name)`, which was the first attempt and does not work: an
    interface name is not a hostname, so it fails for *every* interface on Linux and the
    detection quietly returns None forever. It looked right and was tested against a
    machine with no overlay, where None is also the correct answer — which is how that
    kind of mistake survives.

    Not `ip addr` either: the runtime image has no iproute2, deliberately, and adding a
    package so the node can ask about its own interfaces is the wrong direction.

    Linux-only, and that is where the node runs — the image is Linux. On macOS the ioctl
    number differs and this returns nothing, so a developer gets the same answer as a
    developer with no overlay, which is the truth anyway.
    """
    found: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        for _index, name in socket.if_nameindex():
            address = _address_of(probe, name)
            if address:
                found.append(address)
    return found


def _address_of(probe: socket.socket, name: str) -> str | None:
    try:
        # ifreq: 16 bytes of interface name, then a sockaddr_in whose 4 address bytes
        # sit at offset 20. Padded to 32 so the kernel has room to write the reply.
        reply = fcntl.ioctl(
            probe.fileno(),
            SIOCGIFADDR,
            struct.pack("256s", name.encode()[:15]),
        )
    except OSError:
        # No IPv4 address on this interface, or an ioctl this platform does not have.
        # Both are ordinary: a down interface and macOS each do it.
        return None
    return socket.inet_ntoa(reply[20:24])
