"""`/.well-known/circuless-node` (N12, F2, F15).

What a node says about itself — and, as much, what it does not.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from circuless_node import __version__
from circuless_node.app import UNVERSIONED_PATHS, create_public_app
from circuless_node.settings import NodeOperator, Settings
from circuless_node.well_known import PROTOCOL, node_document

from .harness.keycloak import FixtureRealm

PATH = "/.well-known/circuless-node"


@pytest.fixture
def reachable(realm: FixtureRealm, tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        issuer=realm.issuer,
        overlay_base_url="https://100.64.0.7:8000",
        gateway_base_url="https://n1.nodes.circuless.eu",
        database_url=f"sqlite:///{tmp_path / 'node.db'}",
        data_dir=tmp_path / "data",
    )


@pytest.fixture
def client(reachable: Settings) -> TestClient:
    return TestClient(create_public_app(reachable), raise_server_exceptions=False)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- it needs a token (D21) ---------------------------------------------------------------


def test_it_is_not_anonymous(client: TestClient) -> None:
    """Unusual for a `.well-known` path, and deliberate.

    An unauthenticated description of a node is a free inventory for anyone scanning:
    its version, its overlay address, and confirmation that a CIRCULess node is here.
    Everyone who needs it holds a token.
    """
    assert client.get(PATH).status_code == 401


def test_a_token_for_another_node_does_not_open_it(client: TestClient, realm: FixtureRealm) -> None:
    """The audience check applies here exactly as everywhere else (invariant 3)."""
    other = realm.user_token("alpha.admin", scope="openid node:other-node")
    assert client.get(PATH, headers=bearer(other)).status_code == 401


def test_any_member_may_read_it(client: TestClient, realm: FixtureRealm) -> None:
    """Not an admin-only document. A consumer deciding how to reach this node needs it,
    and everything in it is about the node rather than about anyone's data."""
    response = client.get(PATH, headers=bearer(realm.user_token("alpha.user")))
    assert response.status_code == 200


# --- what it says -----------------------------------------------------------------------


def test_it_identifies_the_node_and_its_realm(
    client: TestClient, realm: FixtureRealm, reachable: Settings
) -> None:
    body = client.get(PATH, headers=bearer(realm.user_token("alpha.user"))).json()
    assert body["protocol"] == PROTOCOL
    assert body["node_id"] == reachable.node_id
    assert body["audience"] == f"node:{reachable.node_id}"
    assert body["issuer"] == reachable.issuer


def test_the_version_is_the_installed_one(client: TestClient, realm: FixtureRealm) -> None:
    """Read from package metadata, not written in a constant.

    It was a constant until N12 and had already drifted — 0.1.0 against pyproject's
    0.2.0 — which nothing noticed because nothing read it. Now the node publishes it
    here and sends it in the heartbeat, so a stale value would be the node telling the
    platform the wrong thing about itself.
    """
    body = client.get(PATH, headers=bearer(realm.user_token("alpha.user"))).json()
    assert body["version"] == __version__
    assert body["version"] != "0+unknown"


def test_the_overlay_comes_before_the_gateway(client: TestClient, realm: FixtureRealm) -> None:
    """Order is the contract: a client tries them in turn, and one that reaches the
    overlay never puts the Cloud on the path at all (§5.6)."""
    body = client.get(PATH, headers=bearer(realm.user_token("alpha.user"))).json()
    assert [endpoint["kind"] for endpoint in body["endpoints"]] == ["overlay", "gateway"]


def test_an_unconfigured_endpoint_is_omitted_not_null(realm: FixtureRealm, tmp_path) -> None:
    """A client should try what is there. "The operator has not set this" is not a thing
    to make every caller handle."""
    settings = Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        issuer=realm.issuer,
        gateway_base_url="https://n1.nodes.circuless.eu",
        database_url=f"sqlite:///{tmp_path / 'n.db'}",
        data_dir=tmp_path / "d",
    )
    document = node_document(settings)
    assert [endpoint["kind"] for endpoint in document["endpoints"]] == ["gateway"]


def test_capabilities_describe_what_can_be_attempted(
    client: TestClient, realm: FixtureRealm
) -> None:
    body = client.get(PATH, headers=bearer(realm.user_token("alpha.user"))).json()
    capabilities = body["capabilities"]
    assert set(capabilities["actions"]) == {"read", "invoke"}
    assert "file" in capabilities["shapes"]
    # No limit is advertised until N19 enforces one: a limit nothing applies is worse
    # than none, because a client sizes an upload against it.
    assert "max_upload_bytes" not in capabilities


def test_it_says_whether_the_node_takes_sensitive_data(
    reachable: Settings, realm: FixtureRealm, tmp_path
) -> None:
    """D22. A provider choosing where to register needs to know before they try, and the
    refusal is a property of the node rather than of their data."""
    assert node_document(reachable)["capabilities"]["accepts_sensitive"] is False

    partner = Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        issuer=realm.issuer,
        operator=NodeOperator.PARTNER,
        database_url=f"sqlite:///{tmp_path / 'p.db'}",
        data_dir=tmp_path / "pd",
    )
    assert node_document(partner)["capabilities"]["accepts_sensitive"] is True


# --- what it must not say -------------------------------------------------------------------


def test_the_document_cannot_know_which_organisations_are_hosted_here() -> None:
    """The one thing in scope that is about other people rather than about the node.

    The Cloud keeps that mapping in its node registry and restricts it to
    `platform-admin`; publishing it to every authenticated caller would route round that.

    Asserted as *unreachable* rather than *absent*: `node_document` takes settings and
    nothing else, so it has no session, no engine and no way to read the tenant table.
    A later change that wanted to add a tenant list would have to change this signature
    first, which is a visible decision rather than a line in a dict.
    """
    import inspect

    parameters = set(inspect.signature(node_document).parameters)
    assert parameters == {"settings", "version"}, (
        "node_document gained a parameter. If it is a database handle, the document can "
        "now disclose which organisations this node hosts — which the Cloud restricts "
        "to platform-admin."
    )


def test_it_names_no_organisation(client: TestClient, realm: FixtureRealm) -> None:
    """The same rule from the other side, against a rendered document.

    The fixture's addresses are deliberately not named after a tenant, so any
    organisation slug appearing here came from the node rather than from configuration.
    """
    body = client.get(PATH, headers=bearer(realm.user_token("alpha.user"))).text.lower()
    for leak in ("alpha", "beta", "tenant", '"org'):
        assert leak not in body, f"the document mentions {leak!r}"


def test_it_carries_no_key_material_or_paths(client: TestClient, realm: FixtureRealm) -> None:
    body = client.get(PATH, headers=bearer(realm.user_token("alpha.user"))).text
    for leak in ("BEGIN", "private", "data_dir", "sqlite", "/Users", "database"):
        assert leak not in body, f"the document mentions {leak!r}"


# --- it is the only thing outside /v1 -----------------------------------------------------


def test_it_is_the_only_public_path_outside_v1() -> None:
    assert {PATH} == UNVERSIONED_PATHS


def test_a_detected_overlay_address_is_published(
    realm: FixtureRealm, tmp_path, monkeypatch
) -> None:
    """N15. The document advertises the address the node is actually on.

    The failure this prevents: a node publishing an address nobody can route to, which
    a client discovers by timing out rather than by being told.
    """
    from circuless_node import overlay

    monkeypatch.setattr(overlay, "_local_addresses", lambda: ["172.17.0.2", "100.92.1.7"])
    settings = Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        issuer=realm.issuer,
        gateway_base_url="https://n1.nodes.circuless.eu",
        database_url=f"sqlite:///{tmp_path / 'n.db'}",
        data_dir=tmp_path / "d",
    )
    document = node_document(settings)
    assert document["endpoints"] == [
        {"kind": "overlay", "url": "https://100.92.1.7:8000"},
        {"kind": "gateway", "url": "https://n1.nodes.circuless.eu"},
    ]


def test_no_overlay_means_the_gateway_alone(realm: FixtureRealm, tmp_path, monkeypatch) -> None:
    from circuless_node import overlay

    monkeypatch.setattr(overlay, "_local_addresses", lambda: ["127.0.0.1"])
    settings = Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        issuer=realm.issuer,
        gateway_base_url="https://n1.nodes.circuless.eu",
        database_url=f"sqlite:///{tmp_path / 'n.db'}",
        data_dir=tmp_path / "d",
    )
    assert [e["kind"] for e in node_document(settings)["endpoints"]] == ["gateway"]
