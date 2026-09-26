"""Storage adapter (N14).

File storage only in the beta. Object storage is a later configuration change, not a
rewrite (FR11, D30) — which is why access goes through `fsspec` rather than `open()`.

Layout is `data_dir/<tenant_id>/<path>`, so a tenant's content is separable on disk even
though isolation itself is enforced in code (§4.3).
"""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath

import fsspec

from .settings import Settings


class PathNotAllowedError(ValueError):
    """A resource path escaped its tenant's directory."""


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

        Refuses `..`, absolute paths, and scheme-relative or absolute URLs (SR-3.2.3).
        """
        candidate = relative_path.strip()
        if not candidate:
            raise PathNotAllowedError("empty path")
        if candidate.startswith("/") or candidate.startswith("\\"):
            raise PathNotAllowedError("absolute paths are not allowed")
        # "//host/x" is scheme-relative and "https://host/x" is absolute; both would send a
        # consumer somewhere other than this node.
        if "//" in candidate or "://" in candidate:
            raise PathNotAllowedError("URLs are not allowed as paths")
        if any(part == ".." for part in PurePosixPath(candidate).parts):
            raise PathNotAllowedError("'..' is not allowed")

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

    def delete(self, tenant_id: uuid.UUID, relative_path: str) -> None:
        target = self.resolve(tenant_id, relative_path)
        if self._fs.exists(target):
            self._fs.rm(target, recursive=True)
