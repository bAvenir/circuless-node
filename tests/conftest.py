from __future__ import annotations

import pytest

from circuless_node.settings import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    """A node configured to write nothing outside the test's own directory."""
    return Settings(  # type: ignore[call-arg]
        node_id="test-node",
        database_url=f"sqlite:///{tmp_path / 'node.db'}",
        data_dir=tmp_path / "data",
        cors_allow_origins=["https://ui.circuless.eu"],
    )
