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
    assert {path.name for path in SCRIPTS} == {"app.js", "api.js", "session.js", "fields.js"}


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


def api_helpers() -> dict[str, str]:
    """Every exported helper in `api.js`, mapped to the path it calls.

    Per helper rather than by scanning for call sites, because the scanning version kept
    going quietly blind: first when a call grew a second argument, then again when
    `send("POST", path)` put a method literal where the path used to be. Reading each
    helper means a helper whose path cannot be found is a failure, not an absence.
    """
    api_js = (ASSETS / "api.js").read_text()
    found: dict[str, str] = {}

    for name, body in re.findall(r"export const (\w+) =(.*?);", api_js, re.S):
        # Skip the HTTP method `send` takes first; the path is the other string literal.
        paths = [lit for lit in re.findall(r'[`"]([^`"]+)[`"]', body) if not lit.isupper()]
        assert paths, f"{name} calls no path this test can read"
        found[name] = paths[0]

    return found


def test_the_helpers_are_all_found() -> None:
    """Guards the reader above, which every path test depends on."""
    assert set(api_helpers()) == {
        "whoami",
        "nodeDocument",
        "tenants",
        "resources",
        "resource",
        "accessLog",
        "createResource",
        "patchResource",
    }


def test_every_api_path_the_ui_calls_is_a_real_route(settings: Settings) -> None:
    """The drift this catches is invisible until someone clicks the thing.

    A renamed route, a changed prefix or a typo in a template literal produces a 404
    that the UI reports as "Not found" — indistinguishable, to the person reading it,
    from a resource that genuinely is not there.
    """
    served = {wildcarded(path) for path in route_paths(create_public_app(settings))}

    unknown = {
        f"{name} -> {path}"
        for name, path in api_helpers().items()
        if wildcarded(path) not in served
    }
    assert not unknown, f"the UI calls paths this node does not serve: {sorted(unknown)}"


def test_every_query_parameter_the_ui_sends_is_accepted(settings: Settings) -> None:
    """The access log's filters are the only query parameters the UI sends.

    A renamed parameter is not an error anywhere: FastAPI ignores what it does not
    declare, so a filter would simply stop filtering and the page would show everything
    while looking like it had narrowed.
    """
    import inspect

    from circuless_node.resources import resource_router

    [route] = [r for r in resource_router().routes if r.path.endswith("/access-log")]
    accepted = set(inspect.signature(route.endpoint).parameters)

    app_js = (ASSETS / "app.js").read_text()
    [call] = re.findall(r"api\.accessLog\(tenant, \{(.*?)\}\)", app_js, re.S)
    # `[A-Za-z_]`, not `[a-z_]`: the first version could not match a camelCase key, so
    # renaming `resource_id` to `resourceId` — exactly the mistake this guards — matched
    # nothing and passed. A pattern that cannot see the error is not a test.
    sent = set(re.findall(r"^\s*([A-Za-z_]+):", call, re.M))

    assert sent, "the pattern matched nothing — this test would pass against any script"
    assert sent <= accepted, f"the UI sends parameters the node ignores: {sorted(sent - accepted)}"


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


def test_the_decision_filter_offers_only_values_the_node_accepts() -> None:
    """The node declares `^(allow|deny)$` and answers 422 to anything else.

    The UI filters the fragment against its own allowlist before sending it, so a value
    that drifted out of step would not 422 — it would be dropped, and the page would
    quietly show every entry while the chip looked selected.
    """
    import inspect

    from circuless_node.resources import resource_router

    [route] = [r for r in resource_router().routes if r.path.endswith("/access-log")]
    declared = inspect.signature(route.endpoint).parameters["decision"].default
    # FastAPI keeps the constraint in pydantic metadata rather than on the Query itself.
    [constraint] = [m for m in declared.metadata if hasattr(m, "pattern")]
    pattern = re.compile(constraint.pattern)

    app_js = (ASSETS / "app.js").read_text()
    [block] = re.findall(r"const DECISIONS = \[(.*?)\];", app_js)
    offered = re.findall(r'"([a-z]+)"', block)

    assert offered, "the pattern matched nothing — this test would pass against any script"
    for value in offered:
        assert pattern.fullmatch(value), f"the UI offers {value!r}, which the node refuses"


def test_the_log_view_reads_only_fields_the_node_returns() -> None:
    """`entry_out` is the contract (N11), and the same drift applies as for resources.

    A renamed field renders as an em dash or as `undefined`, which in a log reads as
    "this entry has no subject" rather than as a bug in the page.
    """
    import uuid as _uuid

    from circuless_node.access_log import entry_out
    from circuless_node.models import AccessLog

    returned = set(
        entry_out(
            AccessLog(
                tenant_id=_uuid.uuid4(),
                request_id="r-1",
                action="read",
                subject_sub="s-1",
                principal_type="user",
                decision="allow",
            )
        )
    )

    app_js = (ASSETS / "app.js").read_text()
    read = set(re.findall(r"\bentry\.([A-Za-z_]+)", app_js))

    assert read, "the pattern matched nothing — this test would pass against any script"
    assert read <= returned, (
        f"the log view reads fields the node does not return: {sorted(read - returned)}"
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


# --- the vocabularies the form hardcodes -----------------------------------------------
#
# `fields.js` duplicates the node's enums because nothing publishes them. The duplication
# is fine; the duplication going stale is not, and a missing Theme would show up as an
# option nobody can pick rather than as an error.


def js_list(name: str) -> list[str]:
    body = (ASSETS / "fields.js").read_text()
    [block] = re.findall(rf"export const {name} = \[(.*?)\];", body, re.S)
    found = re.findall(r'"([^"]+)"', block)
    assert found, f"{name} parsed as empty — the pattern cannot read it"
    return found


def test_the_form_offers_exactly_the_node_s_vocabularies() -> None:
    from circuless_node.models import (
        Classification,
        Discoverability,
        ResourceKind,
        Theme,
        Visibility,
    )
    from circuless_node.vocabularies import LICENCES

    for name, enum in [
        ("KINDS", ResourceKind),
        ("THEMES", Theme),
        ("CLASSIFICATIONS", Classification),
        ("VISIBILITIES", Visibility),
        ("DISCOVERABILITIES", Discoverability),
    ]:
        assert js_list(name) == [member.value for member in enum], name

    assert set(js_list("LICENCES")) == set(LICENCES)


def test_the_shape_choices_exclude_the_one_a_dataset_may_not_have() -> None:
    """`ResourceIn` refuses `shape=service` on a dataset, and forces it on a service.

    So the dropdown is a dataset's choices only — a structural rule, not a value one:
    there is no form in which a person picks `service` as a shape.
    """
    from circuless_node.models import Shape

    assert js_list("DATASET_SHAPES") == [s.value for s in Shape if s is not Shape.SERVICE]


# --- the fields themselves ---------------------------------------------------------------


def form_fields() -> list[dict]:
    """`FIELDS` from `fields.js`, as far as Python needs to read it."""
    body = (ASSETS / "fields.js").read_text()
    [block] = re.findall(r"export const FIELDS = \[(.*?)\n\];", body, re.S)
    entries = re.findall(r"\{(.*?)\}(?=,\n|\n)", block, re.S)
    parsed = []
    for entry in entries:
        [name] = re.findall(r'name: "([^"]+)"', entry)
        kinds = re.findall(r"kinds: \[([^\]]*)\]", entry)
        parsed.append(
            {
                "name": name,
                "kinds": re.findall(r'"([^"]+)"', kinds[0]) if kinds else None,
                "create": "create: true" in entry,
            }
        )
    assert parsed, "FIELDS parsed as empty — the pattern cannot read it"
    return parsed


def test_every_form_field_is_one_the_node_accepts() -> None:
    """A field the node ignores silently does nothing, which looks like it worked."""
    from circuless_node.resources import ResourceIn, ResourcePatch

    for field in form_fields():
        model = ResourceIn if field["create"] else ResourcePatch
        assert field["name"] in model.model_fields, (
            f"{field['name']} is not a field of {model.__name__}"
        )


def test_the_fields_a_create_form_omits_are_not_required_by_the_node() -> None:
    """The inverse: a required field missing from the form cannot be filled in at all."""
    from circuless_node.resources import ResourceIn

    offered = {field["name"] for field in form_fields()}
    required = {name for name, info in ResourceIn.model_fields.items() if info.is_required()}
    assert required <= offered, f"the form cannot supply: {sorted(required - offered)}"


def test_the_kind_split_matches_what_the_node_refuses() -> None:
    """The structural rule, proved against `coherent_for_its_kind` rather than restated.

    A dataset form must be able to produce a valid dataset, and must not offer the
    fields that make one invalid. Both halves are checked by building the payload the
    form would send and handing it to the model.
    """
    import pydantic

    from circuless_node.resources import ResourceIn

    def shown(kind: str) -> set[str]:
        return {
            field["name"]
            for field in form_fields()
            if field["kinds"] is None or kind in field["kinds"]
        }

    base = {"title": "T", "theme": "processing", "classification": "synthetic"}

    dataset = ResourceIn(slug="d", kind="dataset", **base)
    assert dataset.shape.value == "file"

    service = ResourceIn(slug="s", kind="service", endpoint_url="https://upstream.example", **base)
    assert service.shape.value == "service"

    # Each field the form hides for a kind is one the node refuses for that kind.
    for kind, hidden, value in [
        ("dataset", "endpoint_url", "https://upstream.example"),
        ("service", "storage_path", "some/path"),
    ]:
        assert hidden not in shown(kind), f"{kind} should not offer {hidden}"
        extra = {"endpoint_url": "https://upstream.example"} if kind == "service" else {}
        with pytest.raises(pydantic.ValidationError):
            ResourceIn(slug="x", kind=kind, **base, **extra, **{hidden: value})
