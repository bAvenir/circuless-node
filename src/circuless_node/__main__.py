"""Entrypoint. Runs the public and internal applications on their own sockets.

    circuless-node
    uvx circuless-node==<version>

Both servers run in one process and one event loop; if either stops, the process stops,
because a node serving data with no health endpoint — or a health endpoint with no node —
is worse than a node that is plainly down.
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from .app import create_internal_app, create_public_app
from .settings import get_settings


async def _serve() -> None:
    settings = get_settings()

    public = uvicorn.Server(
        uvicorn.Config(
            create_public_app(settings),
            host=settings.public_host,
            port=settings.public_port,
            log_level="info",
            # The node sits behind APISIX on the cloud path, which passes the real client
            # address in X-Forwarded-For.
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    )
    internal = uvicorn.Server(
        uvicorn.Config(
            create_internal_app(settings),
            host=settings.internal_host,
            port=settings.internal_port,
            log_level="warning",
        )
    )

    print(
        f"node {settings.node_id}: public on {settings.public_host}:{settings.public_port}, "
        f"internal on {settings.internal_host}:{settings.internal_port}",
        flush=True,
    )
    await asyncio.gather(public.serve(), internal.serve())


def main() -> int:
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
