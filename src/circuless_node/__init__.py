"""The CIRCULess node."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    #: Read from the installed distribution rather than written here.
    #:
    #: It was hardcoded until N12, and it had already drifted — `0.1.0` against
    #: `pyproject.toml`'s `0.2.0` — which nothing noticed because nothing read it. N12
    #: publishes it in `/.well-known/circuless-node` and N7 sends it in the heartbeat,
    #: so a stale constant would now be a node telling the platform the wrong thing
    #: about itself. `bvr-ci` bumps the manifest; this follows it by construction.
    __version__ = version("circuless-node")
except PackageNotFoundError:  # pragma: no cover — a source checkout with nothing installed
    __version__ = "0+unknown"
