"""Storage adapter, and the path confinement every read and write goes through (N14, H3)."""

from __future__ import annotations

import uuid

import pytest

from circuless_node.settings import Settings
from circuless_node.storage import PathNotAllowedError, Storage

TENANT = uuid.uuid4()
OTHER_TENANT = uuid.uuid4()


@pytest.fixture
def storage(settings: Settings) -> Storage:
    return Storage(settings)


def test_content_lands_under_the_tenant_directory(storage: Storage) -> None:
    with storage.open(TENANT, "batches/timber-001.json", "wb") as handle:
        handle.write(b'{"batch": 1}')

    assert storage.exists(TENANT, "batches/timber-001.json")
    assert storage.size(TENANT, "batches/timber-001.json") == 12
    assert storage.tenant_root(TENANT) in storage.resolve(TENANT, "batches/timber-001.json")
    # Same relative path, different tenant, different file.
    assert not storage.exists(OTHER_TENANT, "batches/timber-001.json")


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",  # the obvious one
        "batches/../../../etc/passwd",  # escaping after a valid prefix
        "/etc/passwd",  # absolute
        "//evil.example/payload",  # scheme-relative URL
        "https://evil.example/payload",  # absolute URL
        "",  # empty
    ],
)
def test_paths_that_leave_the_tenant_directory_are_refused(storage: Storage, path: str) -> None:
    with pytest.raises(PathNotAllowedError):
        storage.resolve(TENANT, path)


def test_a_dot_segment_is_fine(storage: Storage) -> None:
    """Confinement must refuse escapes, not every path containing a dot."""
    resolved = storage.resolve(TENANT, "batches/./timber-001.json")
    assert resolved.endswith("batches/timber-001.json")


def test_delete_removes_content(storage: Storage) -> None:
    with storage.open(TENANT, "scratch.txt", "wb") as handle:
        handle.write(b"x")
    storage.delete(TENANT, "scratch.txt")
    assert not storage.exists(TENANT, "scratch.txt")


def test_delete_is_idempotent(storage: Storage) -> None:
    """Two-stage deletion (N20) purges after a retention period, and a purge job that
    crashes on an already-deleted file would stall behind it."""
    storage.delete(TENANT, "never-existed.txt")
