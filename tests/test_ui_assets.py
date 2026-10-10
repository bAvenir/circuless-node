"""The admin UI's static files, checked the only ways they can be from Python.

There is no browser here and no JavaScript test runner — adding either would be a build
step, which CLAUDE.md rules out. What is left is still worth having, because the failures
it catches are the silent ones: a `getElementById` for an element that was renamed, a
call to an API path the node does not serve, a literal colour that will not flip with the
brand. Each of those renders as a blank area or a dead button rather than as an error.

What this cannot check is behaviour. The sign-in round trip and the rendering are
verified by hand against a real node; see `examples/manual-demo.md`.
"""

from __future__ import annotations

import re

import pytest

from circuless_node.app import create_public_app
from circuless_node.settings import Settings
from circuless_node.ui_staging import UI_SOURCE

from .harness.routes import route_paths

ASSETS = UI_SOURCE / "assets"
SCRIPTS = sorted(ASSETS.glob("*.js"))
INDEX = (UI_SOURCE / "index.html").read_text()


def all_script_text() -> str:
    return "\n".join(path.read_text() for path in SCRIPTS)


def test_there_are_scripts_to_check() -> None:
    """Guards every test below, each of which would pass against an empty glob."""
    assert {path.name for path in SCRIPTS} == {"app.js", "api.js", "session.js"}


# --- the markup and the script agree ---------------------------------------------------


def test_every_element_the_script_looks_up_exists() -> None:
    """A renamed id is a dead button, not an error.

    `getElementById` returns null and the next line throws into a handler that shows
    "something went wrong", which says nothing about the cause.
    """
    wanted = set(re.findall(r'\bel\("([a-z0-9-]+)"\)', all_script_text()))
    wanted |= set(re.findall(r'getElementById\("([a-z0-9-]+)"\)', all_script_text()))
    present = set(re.findall(r'id="([a-z0-9-]+)"', INDEX))

    assert wanted, "the pattern matched nothing — this test would pass against any script"
    assert wanted <= present, (
        f"the script looks up ids that are not in the markup: {sorted(wanted - present)}"
    )


def test_every_view_the_script_toggles_exists() -> None:
    """`VIEWS` drives `show()`; a name in it that is not an element hides nothing."""
    [block] = re.findall(r"const VIEWS = \[(.*?)\];", all_script_text(), re.S)
    views = set(re.findall(r'"([a-z0-9-]+)"', block))
    present = set(re.findall(r'id="([a-z0-9-]+)"', INDEX))

    assert views <= present, f"views with no element: {sorted(views - present)}"


def test_every_asset_the_page_references_is_shipped() -> None:
    for reference in re.findall(r'(?:href|src)="([^"#][^"]*)"', INDEX):
        assert (UI_SOURCE / reference).is_file(), reference


# --- the script and the node agree -----------------------------------------------------


def wildcarded(path: str) -> str:
    """`/v1/t/{tenant_slug}/resources` and `v1/t/${x}/resources` become one string."""
    return "/" + re.sub(r"\$?\{[^}]*\}", "*", path).lstrip("/")


def test_every_api_path_the_ui_calls_is_a_real_route(settings: Settings) -> None:
    """The drift this catches is invisible until someone clicks the thing.

    A renamed route, a changed prefix or a typo in a template literal produces a 404
    that the UI reports as "Not found" — indistinguishable, to the person reading it,
    from a resource that genuinely is not there.
    """
    # `api.js` only: `session.js` calls `params.get("code")` on a URLSearchParams, which
    # the same pattern happily reads as an API path. The test below keeps that narrowing
    # honest by pinning that no other module builds a `v1/` path.
    called = set(re.findall(r'\bget\([`"]([^`"]+)[`"]\)', (ASSETS / "api.js").read_text()))
    served = {wildcarded(path) for path in route_paths(create_public_app(settings))}

    assert called, "the pattern matched nothing — this test would pass against any script"
    unknown = {path for path in called if wildcarded(path) not in served}
    assert not unknown, f"the UI calls paths this node does not serve: {sorted(unknown)}"


def test_api_paths_are_built_in_one_module() -> None:
    """What makes the test above complete rather than merely passing.

    It reads `api.js`; if another module assembled a `v1/` path, that path would go
    unchecked. Keeping them in one place is also why `encodeURIComponent` on a tenant
    slug is applied once rather than remembered at each call site.
    """
    for path in SCRIPTS:
        if path.name == "api.js":
            continue
        assert "v1/" not in path.read_text(), f"{path.name} builds an API path of its own"


def test_the_detail_view_reads_only_fields_the_node_returns() -> None:
    """The same drift as the path test, one level in.

    `resource_out` is the contract. A field renamed there leaves the detail view
    silently skipping a row — the view drops empty values, so a typo looks exactly like
    a resource that has nothing to say.
    """
    import uuid as _uuid

    from circuless_node.models import (
        Classification,
        Resource,
        ResourceKind,
        Shape,
        Theme,
        Visibility,
    )
    from circuless_node.resources import resource_out

    returned = set(
        resource_out(
            Resource(
                tenant_id=_uuid.uuid4(),
                slug="sample",
                kind=ResourceKind.DATASET,
                shape=Shape.FILE,
                title="Sample",
                theme=Theme.MATERIAL_CHARACTERISATION,
                classification=Classification.NON_SENSITIVE,
                licence="CC-BY-4.0",
                visibility=Visibility.ORG,
            )
        )
    )

    app_js = (ASSETS / "app.js").read_text()
    [block] = re.findall(r"const FIELDS = \[(.*?)\n\];", app_js, re.S)
    read = set(re.findall(r"\(r\) => r\.([a-z_]+)", block))
    read |= set(re.findall(r"\bresource\.([a-z_]+)", app_js))

    assert read, "the pattern matched nothing — this test would pass against any script"
    assert read <= returned, (
        f"the detail view reads fields the node does not return: {sorted(read - returned)}"
    )


def test_the_ui_calls_nothing_outside_v1_and_the_identity_provider() -> None:
    """Every `fetch` goes to this node's API, its own config, or the configured issuer.

    A fetch to anywhere else would be an outbound request from a node that may be on a
    partner's premises, which design.md rules out.
    """
    targets = re.findall(r"fetch\(\s*[`\"']([^`\"']*)", all_script_text())
    for target in targets:
        assert (
            target.startswith("../")
            or target == "config.json"
            or target.startswith("${config.issuer}")
        ), target


# --- discipline ---------------------------------------------------------------------------


def test_nothing_is_rendered_as_markup() -> None:
    """Every string the UI shows comes from the node or from a person who named a
    resource. `textContent` makes that safe without anyone having to remember to escape;
    one `innerHTML` is all it takes to stop being true."""
    for path in SCRIPTS:
        body = path.read_text()
        assert "innerHTML" not in body, path.name
        assert "insertAdjacentHTML" not in body, path.name
        assert "document.write" not in body, path.name


def test_the_markup_carries_no_inline_handlers() -> None:
    assert not re.search(r"\son[a-z]+=", INDEX)


def test_the_stylesheet_writes_no_literal_colour() -> None:
    """design.md §1: a hex does not change when the brand does, nor flip with the mode."""
    app_css = (ASSETS / "app.css").read_text()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgb\(|\bhsl\(", app_css)


def test_the_vendored_tokens_match_the_design_pack() -> None:
    """Vendored, not linked (design.md). This is the copy that drifts.

    Skipped outside a full checkout: the design pack lives beside the repository, not
    inside it, so a wheel or a CI job with only this repository cannot compare.
    """
    source = UI_SOURCE.parents[3] / "circuless-design" / "tokens.css"
    if not source.is_file():
        pytest.skip("the design pack is not checked out beside this repository")

    assert (ASSETS / "tokens.css").read_text() == source.read_text(), (
        "circuless-design/tokens.css has changed; re-vendor it into the node's UI"
    )


# --- light mode ------------------------------------------------------------------------


def test_light_mode_is_pinned() -> None:
    """Agreed for N13. `tokens.css` keeps the dark values, so this is a decision the
    markup states rather than a capability the palette lacks."""
    assert 'data-theme="light"' in INDEX
    assert "prefers-color-scheme: dark" in (ASSETS / "tokens.css").read_text()


# --- as served ---------------------------------------------------------------------------


def test_every_script_is_served(tmp_path) -> None:
    from starlette.testclient import TestClient

    node = Settings(  # type: ignore[call-arg]
        node_id="test-node",
        database_url=f"sqlite:///{tmp_path / 'n.db'}",
        data_dir=tmp_path / "data",
    )
    client = TestClient(create_public_app(node), raise_server_exceptions=False)

    for path in SCRIPTS:
        assert client.get(f"/ui/assets/{path.name}").status_code == 200, path.name
