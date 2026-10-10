"""Preparing the directory the admin UI is served from.

The UI ships inside the wheel so a node on a partner's premises renders with no outbound
request of any kind (design.md). But it needs one thing that cannot be shipped: the
issuer and client id its sign-in depends on, which differ per deployment.

Writing that into the installed package would be the obvious move and is the wrong one.
A `uvx` install puts the package in a store that should stay read-only, and the container
runs non-root with the same intent. So the shipped tree is **copied** into the data
directory on every start and the config written beside it. Three consequences, all
wanted: the installed package is never written to, an upgrade cannot leave a stale file
behind because the copy is rebuilt from scratch, and an operator can look at exactly what
their node is serving.

## What may go in `config.json`

It is served anonymously — it has to be, since it is what the browser reads *before* it
has a token — so the rule for `/ui` is narrower than "no secrets":

> No tenant data, no node state, and nothing that is not already public by OAuth design.

An issuer URL and a public client's id qualify: a client id travels in every redirect URL
and the issuer is in every token. A tenant list, a resource name, a count of anything, or
the node's own client credentials do not. `test_ui_staging` asserts the exact key set, so
adding a fifth key fails a test rather than quietly widening what an anonymous caller can
read from this node.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .settings import Settings

#: The UI as shipped. Read-only at runtime; `stage_ui` copies from here.
UI_SOURCE = Path(__file__).parent / "ui"

#: Every key `config.json` may contain. Asserted, because this file is readable without
#: a token and the cost of an extra key is a disclosure nobody reviewed.
CONFIG_KEYS = frozenset({"issuer", "client_id", "node_id", "scope"})


def ui_config(settings: Settings) -> dict[str, str]:
    """What the browser needs before it can obtain a token, and nothing else."""
    return {
        "issuer": settings.issuer,
        "client_id": settings.ui_client_id,
        "node_id": settings.node_id,
        # One audience per token (D14's neighbour: a token naming two audiences is
        # refused). The UI asks for this node and nothing else.
        "scope": f"openid {settings.audience}",
    }


def stage_ui(settings: Settings) -> Path:
    """Copy the shipped UI into the data directory, write `config.json`, return the path.

    Rebuilt from scratch each start rather than synced: a file removed in an upgrade
    must stop being served, and the cheapest way to guarantee that is to not keep the
    old directory.
    """
    staged = settings.data_dir / "ui"
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(UI_SOURCE, staged)

    config = ui_config(settings)
    # Belt and braces for the rule above: the guard is a test, but a mistake here would
    # ship a disclosure, so it also fails at startup rather than at review.
    unexpected = set(config) - CONFIG_KEYS
    if unexpected:
        raise ValueError(f"config.json may not carry {sorted(unexpected)} — see ui_staging")

    (staged / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    return staged
