from __future__ import annotations

import pytest

from circuless_node.settings import Settings

from .harness.keycloak import Admin, FixtureRealm, ensure_keycloak, load_spec


@pytest.fixture
def settings(tmp_path) -> Settings:
    """A node configured to write nothing outside the test's own directory."""
    return Settings(  # type: ignore[call-arg]
        node_id="test-node",
        database_url=f"sqlite:///{tmp_path / 'node.db'}",
        data_dir=tmp_path / "data",
        cors_allow_origins=["https://ui.circuless.eu"],
    )


@pytest.fixture(scope="session")
def keycloak() -> str:
    """A real Keycloak, reused if one is already running (see harness.ensure_keycloak)."""
    return ensure_keycloak()


@pytest.fixture(scope="session")
def realm(keycloak: str) -> FixtureRealm:
    """The fixture realm, rebuilt once per session.

    Session-scoped because building it costs a few seconds and nothing in it is mutated by
    the tests — they only read tokens out of it. A test that needs to change the realm
    should build its own, rather than leaving the shared one altered.
    """
    built = FixtureRealm(Admin(keycloak), load_spec())
    built.rebuild()
    return built


@pytest.fixture
def node_settings(realm: FixtureRealm, tmp_path) -> Settings:
    """Node settings pointed at the fixture realm — what an integration test runs against."""
    return Settings(  # type: ignore[call-arg]
        node_id=realm.node_id,
        issuer=realm.issuer,
        database_url=f"sqlite:///{tmp_path / 'node.db'}",
        data_dir=tmp_path / "data",
        cors_allow_origins=["https://ui.circuless.eu"],
    )
