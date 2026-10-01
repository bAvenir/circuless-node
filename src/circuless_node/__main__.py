"""Entrypoint.

    circuless-node                 run the node
    circuless-node certificate     print this node's certificate, for registration
    circuless-node check           confirm the node can authenticate to the Cloud

Shipped as a container image, pinned by digest and signed with cosign (O18, N15). A
node runs it beside a NetBird agent whose network namespace it shares, so the overlay
interface — and the node's sockets with it — exist only inside that pair and nothing is
published to the host. See `deploy/README.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from . import __version__, overlay
from .app import create_internal_app, create_public_app
from .identity import (
    CloudAuthenticationError,
    CloudCredentials,
    KeyPermissionsError,
    load_or_create_keypair,
)
from .settings import Settings, get_settings
from .sync import SyncState, sync_loop


async def _serve(settings: Settings) -> None:
    internal_app = create_internal_app(settings)
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
            internal_app,
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
    # Both servers and the sync loop share one process and one event loop; if any of
    # them stops, the process stops. A node serving data with no health endpoint — or a
    # health endpoint with no node — is worse than a node that is plainly down.
    #
    # The loop is a peer of the servers, not a subordinate of one. Syncing is one of the
    # three things this process does, and putting it in a server's lifespan would make
    # it look like part of serving requests, which it is not.
    #
    # It shares the internal app's SyncState so that /metrics reports what the loop
    # actually did, rather than a second copy that agrees by coincidence.
    state: SyncState = internal_app.state.sync_state
    await asyncio.gather(
        public.serve(),
        internal.serve(),
        sync_loop(settings, internal_app.state.engine, state, version=__version__),
    )


def _serve_command(settings: Settings) -> int:
    # Generated on first start, so a fresh install has an identity before it has a
    # request to answer — and so the operator has a certificate to register.
    keypair = load_or_create_keypair(settings)
    print(f"node certificate: {keypair.certificate_path} ({keypair.fingerprint[:16]}…)")
    try:
        asyncio.run(_serve(settings))
    except KeyboardInterrupt:
        return 0
    return 0


def _certificate_command(settings: Settings) -> int:
    """Print the certificate an operator registers against this node's Keycloak client.

    Enrollment is manual in the beta (§3.6). Printing the certificate — rather than
    asking someone to find a file — is the difference between a documented step and a
    step people improvise.
    """
    keypair = load_or_create_keypair(settings)
    print(keypair.certificate_pem(), end="")
    print(f"# client_id:   {settings.node_client_id}", file=sys.stderr)
    print(f"# fingerprint: {keypair.fingerprint}", file=sys.stderr)
    print(f"# private key: {keypair.key_path} (stays here, always)", file=sys.stderr)
    return 0


def _check_command(settings: Settings) -> int:
    """Is this node working? One command, because during an install it is the question.

    Three answers, reported together rather than as three tools: the keypair exists and
    is readable only by us, the Cloud accepts us, and we are on the overlay. Each is a
    different person's fault when it fails, which is exactly why someone installing a
    node should not have to know which to ask first.

    Overlay absence is **not** a failure. A node reaches Keycloak and the Cloud API over
    the public internet; the overlay is for inbound traffic only, so a node without one
    works and simply cannot be reached. Exiting non-zero for it would make a correct
    development node look broken.
    """
    keypair = load_or_create_keypair(settings)
    print(f"keypair:   {keypair.key_path} ({keypair.fingerprint[:16]}…)")

    # Reported, not raised. An unregistered certificate is the normal state of a fresh
    # install, and letting it abort the command would mean nobody sees their overlay
    # status until the step before it is done — at exactly the point they are trying to
    # work out which step is missing.
    failed = False
    try:
        CloudCredentials(settings, keypair).token()
        print(f"cloud:     {settings.node_client_id} obtained a circuless-cloud token")
    except CloudAuthenticationError as refusal:
        print(f"cloud:     FAILED — {refusal}", file=sys.stderr)
        failed = True

    detected = overlay.detect_overlay_address()
    if detected is None:
        print("overlay:   not joined — nothing can reach this node")
        print("           (fine for development; for a deployment, check the peer is")
        print("            connected and approved in the NetBird console)")
    else:
        print(f"overlay:   {detected}")

    published = settings.effective_overlay_base_url
    if settings.overlay_base_url and detected and detected not in settings.overlay_base_url:
        # The one combination that is wrong rather than merely incomplete: the node is
        # telling every peer that reads /.well-known to use an address it is not on.
        print(
            f"           WARNING: publishing {settings.overlay_base_url!r}, "
            f"but this node is at {detected}",
            file=sys.stderr,
        )
    print(f"published: {published or '(no overlay address in /.well-known)'}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="circuless-node",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "certificate", "check"],
    )
    args = parser.parse_args()
    settings = get_settings()

    command = {
        "serve": _serve_command,
        "certificate": _certificate_command,
        "check": _check_command,
    }[args.command]

    try:
        return command(settings)
    except (KeyPermissionsError, CloudAuthenticationError) as failure:
        # Both of these are conditions we anticipated and have advice for — a loosened key
        # file, a certificate nobody registered yet. Handing an operator a stack trace for
        # something we can name and tell them how to fix is a poor way to meet them on
        # what is usually their first install.
        print(f"{args.command} failed: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
