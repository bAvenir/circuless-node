"""Staging the admin UI, and what `config.json` is allowed to say.

`config.json` is the one file on this node a caller can read without a token — it has to
be, since it is what the browser reads *before* it has one. So the tests that matter here
are not about staging mechanics. They are about the disclosure: exactly four keys, no
secrets, and a failure if anyone widens that later.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from circuless_node.app import create_public_app
from circuless_node.settings import Settings
from circuless_node.ui_staging import CONFIG_KEYS, UI_SOURCE, stage_ui, ui_config


@pytest.fixture
def node(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        node_id="test-node",
        issuer="https://keycloak.example/realms/circuless",
        database_url=f"sqlite:///{tmp_path / 'node.db'}",
        data_dir=tmp_path / "data",
    )


# --- the disclosure ------------------------------------------------------------------


def test_the_config_carries_exactly_the_four_documented_keys(node: Settings) -> None:
    """The whole guard, in one line.

    A fifth key is a disclosure nobody reviewed, and the obvious ones to reach for —
    a tenant list to save a round trip, the node's status, a count of resources — are
    exactly what must not be here.
    """
    assert set(ui_config(node)) == CONFIG_KEYS == {"issuer", "client_id", "node_id", "scope"}


def test_the_config_carries_nothing_confidential(node: Settings) -> None:
    """Neither the node's own client nor anything derived from its key material.

    `node_client_id` is the confidential client the node authenticates as; the UI's is a
    separate public one. Shipping the wrong one anonymously would name a client whose
    credential is worth having.
    """
    rendered = json.dumps(ui_config(node))
    assert node.node_client_id not in rendered
    assert "fernet" not in rendered.lower()
    assert "secret" not in rendered.lower()
    assert "password" not in rendered.lower()


def test_the_scope_names_this_node_alone(node: Settings) -> None:
    """One audience per token. A token naming two is refused at the door (N2)."""
    assert ui_config(node)["scope"] == "openid node:test-node"
    assert ui_config(node)["scope"].count("node:") == 1


def test_the_ui_client_is_not_the_node_client(node: Settings) -> None:
    assert ui_config(node)["client_id"] == "circuless-ui"
    assert ui_config(node)["client_id"] != node.node_client_id


# --- staging -------------------------------------------------------------------------


def test_it_stages_into_the_data_directory_and_not_the_package(node: Settings) -> None:
    """A `uvx` store and the container's rootfs should both stay read-only."""
    staged = stage_ui(node)

    assert staged == node.data_dir / "ui"
    assert (staged / "config.json").is_file()
    assert not (UI_SOURCE / "config.json").exists(), "the shipped tree was written to"


def test_it_rebuilds_rather_than_merges(node: Settings) -> None:
    """A file dropped in an upgrade must stop being served.

    Syncing would leave it behind, and the file most likely to linger is the one a
    previous version served at a path the new one no longer controls.
    """
    staged = stage_ui(node)
    (staged / "leftover.js").write_text("// from an older version")

    assert not (stage_ui(node) / "leftover.js").exists()


def test_a_second_start_does_not_fail_on_the_existing_directory(node: Settings) -> None:
    """Restarting a node is not an exceptional event."""
    stage_ui(node)
    stage_ui(node)
    assert (node.data_dir / "ui" / "index.html").is_file()


def test_the_config_follows_the_settings(tmp_path) -> None:
    """Two nodes on one machine must not be handed each other's issuer."""
    other = Settings(  # type: ignore[call-arg]
        node_id="other-node",
        issuer="https://elsewhere.example/realms/x",
        ui_client_id="some-other-ui",
        database_url=f"sqlite:///{tmp_path / 'o.db'}",
        data_dir=tmp_path / "other",
    )
    written = json.loads((stage_ui(other) / "config.json").read_text())

    assert written["issuer"] == "https://elsewhere.example/realms/x"
    assert written["client_id"] == "some-other-ui"
    assert written["scope"] == "openid node:other-node"


# --- as served -----------------------------------------------------------------------


def test_the_whole_tree_is_served_without_a_token(node: Settings) -> None:
    """The exemption in practice. Every file the browser needs before it can sign in."""
    client = TestClient(create_public_app(node), raise_server_exceptions=False)

    for path in ("/ui/", "/ui/config.json", "/ui/assets/app.js", "/ui/assets/app.css"):
        assert client.get(path).status_code == 200, path


def test_the_served_config_is_the_staged_one(node: Settings) -> None:
    client = TestClient(create_public_app(node), raise_server_exceptions=False)
    assert client.get("/ui/config.json").json() == ui_config(node)


def test_the_api_is_still_closed(node: Settings) -> None:
    """The exemption must not have widened while nobody was looking."""
    client = TestClient(create_public_app(node), raise_server_exceptions=False)
    assert client.get("/v1/tenants").status_code == 401
    assert client.get("/v1/whoami").status_code == 401


# --- the contract between the file and the script --------------------------------------


def test_the_script_reads_exactly_the_keys_the_node_writes() -> None:
    """Crude, and it has caught this class of drift before.

    `config.json` is written in Python and read in JavaScript, with no shared type and
    no test that exercises both. Renaming a key on one side is a silent failure that
    shows up as a blank page at sign-in.
    """
    import re

    script = (UI_SOURCE / "assets" / "app.js").read_text()
    # Minus the filename: the same pattern matches it in `fetch("config.json")`.
    read = set(re.findall(r"\bconfig\.([a-z_]+)", script)) - {"json"}

    assert read, "the pattern matched nothing — this test would pass against any script"
    assert read <= CONFIG_KEYS, (
        f"the script reads keys the node does not write: {sorted(read - CONFIG_KEYS)}"
    )
