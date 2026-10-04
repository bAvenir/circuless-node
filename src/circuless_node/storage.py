"""Storage adapter (N14).

File storage only in the beta. Object storage is a later configuration change, not a
rewrite (FR11, D30) — which is why access goes through `fsspec` rather than `open()`.

Layout is `data_dir/<tenant_id>/<path>`, so a tenant's content is separable on disk even
though isolation itself is enforced in code (§4.3).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

import fsspec

from .settings import Settings


class PathNotAllowedError(ValueError):
    """A resource path escaped its tenant's directory."""


@dataclass(frozen=True)
class StoredObject:
    """One object in a bucket, as the manifest reports it (N8)."""

    #: Relative to the resource's `storage_path`, so it can be handed straight back as
    #: `/data/{path}`. That is the whole point of the manifest: it is actionable.
    path: str
    size: int
    modified: datetime


def check_relative_path(candidate: str) -> str:
    """The grammar half of path confinement: refuse `..`, absolute paths and URLs.

    Separate from `resolve` because a caller that **joins** a caller-supplied segment
    onto a stored prefix has to check the segment *before* the join. `PurePosixPath`
    collapses repeated slashes, so joining first turns `https://evil/x` into the
    perfectly ordinary `https:/evil/x` and the URL rule never fires on what was actually
    sent (N8 found this the honest way, with a failing test).

    Confinement itself does not depend on these rules — `resolve`'s `is_relative_to`
    backstop does, and it holds whatever the grammar lets through. These exist so that a
    path which was *trying* to leave is refused by name rather than wandering off and
    becoming a 404 about a file that was never going to be there.
    """
    cleaned = candidate.strip()
    if not cleaned:
        raise PathNotAllowedError("empty path")
    if cleaned.startswith("/") or cleaned.startswith("\\"):
        raise PathNotAllowedError("absolute paths are not allowed")
    # "//host/x" is scheme-relative and "https://host/x" is absolute; both would send a
    # consumer somewhere other than this node.
    if "//" in cleaned or "://" in cleaned:
        raise PathNotAllowedError("URLs are not allowed as paths")
    if any(part == ".." for part in PurePosixPath(cleaned).parts):
        raise PathNotAllowedError("'..' is not allowed")
    return cleaned


class Storage:
    def __init__(self, settings: Settings) -> None:
        self._root = settings.data_dir.expanduser().resolve()
        self._fs = fsspec.filesystem("file", auto_mkdir=True)

    @property
    def root(self) -> str:
        return str(self._root)

    def tenant_root(self, tenant_id: uuid.UUID) -> str:
        return str(self._root / str(tenant_id))

    def resolve(self, tenant_id: uuid.UUID, relative_path: str) -> str:
        """Turn a caller-supplied path into an absolute one inside the tenant's directory.

        Path confinement belongs to H3 and is tested there against `/data/{path}` and
        `/invoke/{path}`, but it is done here as well: every read and write in the node
        goes through this function, so this is the one place that cannot be bypassed by a
        handler that forgot.
        """
        candidate = check_relative_path(relative_path)

        tenant_root = (self._root / str(tenant_id)).resolve()
        resolved = (tenant_root / candidate).resolve()
        # Normalising above should make this unreachable; it is the backstop that makes the
        # guarantee independent of the checks being exhaustive.
        if not resolved.is_relative_to(tenant_root):
            raise PathNotAllowedError("path escapes the tenant directory")
        return str(resolved)

    def open(self, tenant_id: uuid.UUID, relative_path: str, mode: str = "rb"):
        return self._fs.open(self.resolve(tenant_id, relative_path), mode)

    def exists(self, tenant_id: uuid.UUID, relative_path: str) -> bool:
        return bool(self._fs.exists(self.resolve(tenant_id, relative_path)))

    def size(self, tenant_id: uuid.UUID, relative_path: str) -> int:
        return int(self._fs.size(self.resolve(tenant_id, relative_path)))

    def list_objects(
        self, tenant_id: uuid.UUID, relative_path: str, limit: int
    ) -> tuple[list[StoredObject], bool]:
        """Every object under a bucket's directory, and whether the list was cut short.

        **Recursive.** A measurement campaign has subdirectories, and a one-level listing
        would make everything below them unreachable — the consumer would have to guess
        paths that the manifest exists to tell them.

        **Capped rather than paged.** Paging a filesystem listing stably needs a cursor,
        and nothing in the beta holds enough objects to need one. The cap is not about
        paging though: it stops a `storage_path` pointed somewhere wrong by mistake from
        turning one request into a several-hundred-megabyte JSON response.

        A missing directory is an empty list, not an error. A bucket registered before
        anything has been uploaded to it is a legitimate state, and the caller has
        already been told they may read it.
        """
        root = self.resolve(tenant_id, relative_path)
        if not self._fs.exists(root):
            return [], False

        found = self._fs.find(root, detail=True)
        # Sorted by path, so a client diffing two manifests sees real changes rather than
        # whatever order the filesystem happened to walk in.
        paths = sorted(found)
        truncated = len(paths) > limit

        objects = []
        for absolute in paths[:limit]:
            info = found[absolute]
            if info.get("type") != "file":
                continue
            objects.append(
                StoredObject(
                    path=str(PurePosixPath(absolute).relative_to(PurePosixPath(root))),
                    size=int(info.get("size") or 0),
                    modified=datetime.fromtimestamp(float(info.get("mtime") or 0), tz=UTC),
                )
            )
        return objects, truncated

    def delete(self, tenant_id: uuid.UUID, relative_path: str) -> None:
        target = self.resolve(tenant_id, relative_path)
        if self._fs.exists(target):
            self._fs.rm(target, recursive=True)
