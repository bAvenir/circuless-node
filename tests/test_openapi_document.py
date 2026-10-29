"""The committed `openapi.json`, and whether it still describes the code.

The public application does not serve its schema, so this file is the only published
description of the node's API — the documentation site renders the platform's API
surface from it. A stale copy is worse than none: it is wrong with an air of authority.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from circuless_node.openapi import DEFAULT_PATH, rendered, schema


def test_the_committed_document_matches_the_code() -> None:
    """Regenerate and compare. Run `circuless-node-openapi` if this fails."""
    assert DEFAULT_PATH.exists(), "openapi.json is not committed"
    assert DEFAULT_PATH.read_text() == rendered(), (
        "openapi.json is out of date — run `uv run circuless-node-openapi`"
    )


def test_the_document_is_the_same_on_every_run() -> None:
    """**The bug this replaces.** `/invoke/{upstream_path}` is declared once for seven
    methods, and FastAPI builds its operation id from an unordered set of them. Four
    consecutive runs produced four different ids, so the file churned on every
    regeneration and the check above would have been noise people re-ran until it
    passed.

    **In separate processes, with different hash seeds.** Two calls in one process
    agree whatever the code does: FastAPI caches `app.openapi()`, and Python's set
    ordering is fixed for the life of a process. An in-process comparison passes with
    the normalisation removed — measured, which is why this shells out.
    """
    runs = {
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-c",
                "from circuless_node.openapi import rendered; print(rendered())",
            ],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("0", "1", "12345")
    }

    assert len(runs) == 1, "the document differs between runs; operation ids are unstable"


def test_every_operation_id_is_unique() -> None:
    """FastAPI gave all seven methods of the invoke route the **same** id, which breaks
    any client generated from this document."""
    # The **rendered** document, not the helper's output. Asserting on
    # `with_stable_operation_ids(schema())` tests the function and not the pipeline:
    # with the call removed from `rendered()` this still passed.
    document = json.loads(rendered())
    ids = [
        operation["operationId"]
        for operations in document["paths"].values()
        for operation in operations.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]

    assert ids, "no operations found; the extraction has stopped matching"
    assert len(ids) == len(set(ids)), f"duplicate operation ids: {sorted(set(ids) ^ set(ids))}"


def test_it_describes_the_public_application_only() -> None:
    """`/internal/authz`, `/metrics` and `/healthz` live on the internal application,
    which binds loopback or the overlay and is never routed by the gateway. Publishing
    them here would invite somebody to try them through the gateway (R8, D21)."""
    paths = set(schema()["paths"])

    for internal in ("/internal/authz", "/metrics", "/healthz"):
        assert internal not in paths, f"{internal} must not be in the published document"


def test_no_local_configuration_leaks_into_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one required setting is `node_id`, and the exporter pins it with
    `_env_file=None`. A document that differed between developers, or between a laptop
    and CI, would make the drift check unusable.

    Asserted by **setting the environment and checking nothing moves**, which is the
    property that matters rather than the mechanism that currently provides it.

    Honest limitation: removing `_env_file=None` does not fail this test, because
    `node_id` is passed explicitly — so the environment cannot reach it — and no other
    setting is visible in the schema today. The flag is defensive against a future
    setting that is, and against a developer `.env`. If one ever appears, this test
    starts earning its keep without being changed.
    """
    baseline = json.dumps(schema(), sort_keys=True)

    monkeypatch.setenv("CIRCULESS_NODE_NODE_ID", "a-different-node")
    monkeypatch.setenv("CIRCULESS_NODE_OPERATOR", "partner")

    assert json.dumps(schema(), sort_keys=True) == baseline, (
        "the environment reached the document; `_env_file=None` is not enough on its own"
    )
    assert "a-different-node" not in baseline


def test_every_published_path_is_versioned_or_well_known() -> None:
    """Invariant 1. Everything the gateway can reach lives under `/v1`; the single
    exception is `/.well-known/circuless-node`, which needs a platform token."""
    for path in schema()["paths"]:
        assert path.startswith(("/v1/", "/.well-known/")), path
