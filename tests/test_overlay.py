"""Finding this node's overlay address (N15).

The detection itself is one `ioctl` per interface, so these drive `_local_addresses` and
assert the decision made on top of it. The syscall is covered where it has to be — on
Linux, against a real interface in the overlay range — which a macOS test run cannot do:

    docker run --rm --cap-add NET_ADMIN -v "$PWD/src:/src:ro" python:3.12-slim-trixie sh -c '
      apt-get update -qq && apt-get install -y -qq iproute2
      ip link add wt0 type dummy && ip addr add 100.92.1.7/16 dev wt0 && ip link set wt0 up
      python -c "import sys; sys.path.insert(0,\\"/src\\")
      from circuless_node.overlay import detect_overlay_address; print(detect_overlay_address())"'

That is how the first implementation was caught. It used `getaddrinfo(interface_name)`,
which is not a thing — it fails for every interface on Linux, so detection returned None
always. It passed every test written against a machine with no overlay, where None is
also the right answer.
"""

from __future__ import annotations

import pytest

from circuless_node import overlay
from circuless_node.settings import Settings


@pytest.fixture
def interfaces(monkeypatch):
    """Stand in for the host's interfaces."""

    def set_to(*addresses: str) -> None:
        monkeypatch.setattr(overlay, "_local_addresses", lambda: list(addresses))

    return set_to


# --- which addresses count ----------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["100.64.0.1", "100.92.1.7", "100.127.255.254"],
)
def test_an_address_in_the_overlay_range_is_found(interfaces, address: str) -> None:
    interfaces("127.0.0.1", "172.17.0.2", address)
    assert overlay.detect_overlay_address() == address


@pytest.mark.parametrize(
    ("address", "why"),
    [
        ("100.63.255.255", "just below the range"),
        ("100.128.0.1", "just above the range"),
        ("10.0.0.5", "ordinary private"),
        ("172.17.0.2", "the Docker bridge, which shares the namespace"),
        ("192.168.1.10", "a home LAN"),
        ("127.0.0.1", "loopback"),
    ],
)
def test_an_address_outside_the_range_is_not(interfaces, address: str, why: str) -> None:
    """The boundaries matter: 100.64.0.0/10 is 100.64 through 100.127, which is not the
    obvious reading of the prefix and is easy to implement as /16 or /8 by accident."""
    interfaces(address)
    assert overlay.detect_overlay_address() is None, why


def test_no_overlay_is_an_ordinary_answer(interfaces) -> None:
    """Not an error. A node reaches Keycloak and the Cloud API over the public internet;
    the overlay is inbound only, so a node without one works and cannot be reached."""
    interfaces("127.0.0.1", "192.168.1.10")
    assert overlay.detect_overlay_address() is None


def test_the_docker_bridge_is_not_mistaken_for_the_overlay(interfaces) -> None:
    """Both live in the same namespace, by design — the bridge is how the node reaches
    Keycloak. Publishing the bridge address in /.well-known would advertise an address
    no peer can route to."""
    interfaces("172.17.0.2", "100.92.1.7")
    assert overlay.detect_overlay_address() == "100.92.1.7"


def test_the_url_carries_the_public_port(interfaces) -> None:
    interfaces("100.92.1.7")
    assert overlay.overlay_base_url(8000) == "http://100.92.1.7:8000"


def test_the_overlay_url_is_http_not_https(interfaces) -> None:
    """D28: TLS on publicly reachable endpoints, WireGuard on the overlay.

    The node terminates no TLS on either socket, so advertising `https` sends a client
    following §5.6's "try overlay first" into a TLS handshake against a plain socket.

    Pinned as its own test because it reads like a typo waiting to be helpfully
    corrected. It was `https` until the Q3 dry run measured both halves: a 200 over
    `http://100.98.65.67:8000`, while the document advertised `https` for that same
    address.
    """
    interfaces("100.92.1.7")
    assert overlay.overlay_base_url(8000).startswith("http://")


def test_an_explicit_override_is_published_verbatim(interfaces, tmp_path) -> None:
    """Including its scheme. An override exists for what detection cannot know about —
    a relay, or an address something else terminates TLS for — so the node must not
    rewrite it to match its own socket."""
    interfaces("100.92.1.7")
    settings = Settings(  # type: ignore[call-arg]
        node_id="n",
        overlay_base_url="https://relay.example:443",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'n.db'}",
    )
    assert settings.effective_overlay_base_url == "https://relay.example:443"


def test_no_url_without_an_address(interfaces) -> None:
    interfaces("127.0.0.1")
    assert overlay.overlay_base_url(8000) is None


def test_a_malformed_address_is_skipped_not_raised(interfaces) -> None:
    """`_local_addresses` reads bytes out of an ioctl reply. A surprise there should
    cost one interface, not the node's ability to describe itself."""
    interfaces("not-an-address", "100.92.1.7")
    assert overlay.detect_overlay_address() == "100.92.1.7"


# --- what the node publishes ----------------------------------------------------------------


def test_the_detected_address_is_published(interfaces, tmp_path) -> None:
    interfaces("100.92.1.7")
    settings = Settings(  # type: ignore[call-arg]
        node_id="n", data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'n.db'}"
    )
    assert settings.effective_overlay_base_url == "http://100.92.1.7:8000"


def test_the_setting_overrides_detection(interfaces, tmp_path) -> None:
    """For what detection cannot know about — a relay, or an address a gateway rewrites."""
    interfaces("100.92.1.7")
    settings = Settings(  # type: ignore[call-arg]
        node_id="n",
        overlay_base_url="https://node.example:443",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'n.db'}",
    )
    assert settings.effective_overlay_base_url == "https://node.example:443"


def test_nothing_is_published_when_there_is_no_overlay(interfaces, tmp_path) -> None:
    """Omitted rather than guessed. A document advertising an unreachable address is
    worse than one advertising none, because a client will try it."""
    interfaces("127.0.0.1")
    settings = Settings(  # type: ignore[call-arg]
        node_id="n", data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'n.db'}"
    )
    assert settings.effective_overlay_base_url is None


def test_it_is_not_cached(interfaces, tmp_path) -> None:
    """A node that joins the overlay a minute after starting should say so without a
    restart — and during an install, that minute is most of the install."""
    settings = Settings(  # type: ignore[call-arg]
        node_id="n", data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'n.db'}"
    )
    interfaces("127.0.0.1")
    assert settings.effective_overlay_base_url is None

    interfaces("100.92.1.7")
    assert settings.effective_overlay_base_url == "http://100.92.1.7:8000"
