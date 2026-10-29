"""Writing the node's OpenAPI document to a file.

The public application does **not** serve its schema — `create_public_app` sets
`openapi_url=None`, because an unauthenticated index of every route is a gift to anyone
scanning, and this socket is what the gateway and the overlay reach. So the document is
produced by running the application and asking it, and the result is committed.

## Why committed rather than served or uploaded

**Review.** An API change shows up as a diff in the pull request that makes it. A
reviewer sees a path disappear or a field become optional without reading the Python.

**The documentation site.** `circuless-platform-beta` renders the platform's API surface
from this file and the Cloud's. A build artifact would be ephemeral and need credentials
to fetch across repositories; a file on `main` needs neither.

CI regenerates and fails if the committed copy differs, so it cannot go stale quietly.

## The public application only

`create_internal_app` is deliberately absent. It carries `/internal/authz`, `/metrics`
and `/healthz`, binds loopback or the overlay, and is never routed by the gateway. It
serves its own `/docs` there, for an operator who is already on the host. Publishing its
surface alongside the public one would invite somebody to try those paths through the
gateway and wonder why they 404.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .app import create_public_app
from .settings import Settings

#: Beside the project root, so a consumer fetching it needs one path.
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "openapi.json"

#: A fixed, obviously fake node id. `node_id` is the one required setting, and the
#: document must not vary with whose machine produced it — a schema that differs
#: between developers turns the drift check into noise people re-run until it passes.
PLACEHOLDER_NODE_ID = "example-node"


def schema() -> dict[str, Any]:
    """The document FastAPI builds from the public application's routes and models.

    Settings are constructed explicitly, with `_env_file=None`, so that a developer's
    own `.env` cannot leak into a committed file.
    """
    settings = Settings(node_id=PLACEHOLDER_NODE_ID, _env_file=None)  # type: ignore[call-arg]
    return create_public_app(settings).openapi()


#: Everything an operation id may contain, after squashing the rest to underscores.
_SAFE = re.compile(r"[^a-zA-Z0-9]+")


def with_stable_operation_ids(document: dict[str, Any]) -> dict[str, Any]:
    """Give every operation an id derived from its method and path.

    **FastAPI's own ids are not stable here.** `/invoke/{upstream_path}` is declared
    once for seven methods, and FastAPI builds the id from an unordered set of them —
    so four consecutive runs of this exporter produced `…__put`, `…__patch`,
    `…__delete` and `…__options`. A committed file that changes on every run makes the
    drift check noise that people re-run until it passes.

    It is also wrong in a second way: all seven operations shared **one** id, and a
    duplicate operation id breaks any generated client.

    Derived from the method and the path, so it is stable, unique, and legible.
    """
    for path, operations in document.get("paths", {}).items():
        slug = _SAFE.sub("_", path).strip("_").lower()
        for method, operation in operations.items():
            if isinstance(operation, dict) and "operationId" in operation:
                operation["operationId"] = f"{method.lower()}_{slug}"
    return document


def rendered(document: dict[str, Any] | None = None) -> str:
    """The exact bytes that belong on disk.

    Sorted and indented: a diff of this file should be a diff of the API, not of
    whatever order FastAPI happened to walk its routes in.
    """
    return (
        json.dumps(with_stable_operation_ids(document or schema()), indent=2, sort_keys=True) + "\n"
    )


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    path.write_text(rendered())
    print(f"{path}: {len(schema()['paths'])} paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
