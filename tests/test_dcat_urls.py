"""Where a published record tells a consumer to go.

Two faults lived here undetected because the only assertions were
`accessURL.endswith("/data")` and `endpointURL.endswith("/invoke")`, which hold
whatever the host is and whether or not the path is one this node serves.

**The host was wrong.** Both URLs were built from `resource_uri`, whose host was then a
fixed `{node_id}.circuless.bavenir.eu` pattern that came from nothing configurable. A
node is published at `gateway_base_url` — `…nodes.circuless.bavenir.eu` in the beta — so
a browser following the record went somewhere nothing answered.

**The path was wrong.** It omitted `/v1`, which every route on the node carries
(invariant 1). Even on the right host it would have been a 404.

A third followed from the same root: the identifier was *spelled* like an address, so
it kept getting used as one — as the fallback `accessURL` host, and as `dcat:landingPage`
outright. It is now minted under one project namespace the project owns, with the node as
a path segment and not a hostname, which is a shape nobody can mistake for somewhere to
GET. `dcat:landingPage` is no longer published at all: the replacement pointed at the
admin UI under `/ui`, which the Cloud gateway does not route.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

import pytest

from circuless_node.app import create_public_app
from circuless_node.dcat import (
    IDENTIFIER_BASE,
    OPENAPI_DOCUMENT,
    TERM_BASE,
    THEME_BASE,
    access_url,
    invoke_url,
    openapi_url,
    render,
    resource_uri,
)
from circuless_node.models import (
    Classification,
    Discoverability,
    Resource,
    ResourceKind,
    Shape,
    Theme,
    Visibility,
)
from circuless_node.settings import Settings

from .harness.routes import route_paths

RESOURCE = uuid.UUID("7f3a9c21-4e5b-4c8a-9d2e-1b0f6a8c3d54")
GATEWAY = "https://alpha.nodes.circuless.bavenir.eu"


def dataset() -> Resource:
    return Resource(
        id=RESOURCE,
        tenant_id=uuid.uuid4(),
        slug="batch-7",
        kind=ResourceKind.DATASET,
        shape=Shape.FILE,
        title="Recycled PET batch 7",
        theme=Theme.MATERIAL_CHARACTERISATION,
        classification=Classification.NON_SENSITIVE,
        licence="CC-BY-4.0",
        discoverability=Discoverability.CATALOGUE,
        visibility=Visibility.AGREEMENT,
    )


def service() -> Resource:
    resource = dataset()
    resource.kind = ResourceKind.SERVICE
    resource.shape = Shape.SERVICE
    resource.endpoint_url = "http://upstream.internal:8080"
    return resource


# --- the identifier and the address are different things ---------------------------------


def test_the_identifier_does_not_move_with_the_deployment() -> None:
    """`@id` is deterministic and depends on nothing configurable, which is what makes
    it an identifier. A record lifted out of the catalogue still says where it came
    from, whatever hostname that node is published at today."""
    record = render(dataset(), node_id="alpha-onprem", tenant_slug="alpha", base_url=GATEWAY)

    assert record["@id"] == resource_uri("alpha-onprem", "alpha", RESOURCE)
    # Same resource, same name, from a node published somewhere else entirely.
    elsewhere = render(
        dataset(), node_id="alpha-onprem", tenant_slug="alpha", base_url="https://other.example"
    )
    assert elsewhere["@id"] == record["@id"]


def test_the_identifier_names_the_node_without_pretending_to_be_it() -> None:
    """The whole fix. The node used to be the *hostname*, which read as an address,
    resolved nowhere, and was twice used as one. It is now a path segment under a
    namespace the project owns."""
    uri = resource_uri("alpha-onprem", "alpha", RESOURCE)

    assert urlparse(uri).hostname == "circuless.bavenir.eu"
    assert "alpha-onprem" not in (urlparse(uri).hostname or "")
    assert "/nodes/alpha-onprem/" in uri
    assert uri.startswith(IDENTIFIER_BASE)


def test_the_cloud_can_still_recover_the_resource_id_from_it() -> None:
    """`catalogue.parse_record` on the Cloud side takes the last path segment as the
    resource UUID, because C6's agreements name a resource rather than a record. Any
    change to the identifier's shape has to keep that true, so this is that parse."""
    uri = resource_uri("alpha-onprem", "alpha", RESOURCE)

    assert uuid.UUID(uri.rstrip("/").rsplit("/", 1)[-1]) == RESOURCE


def test_the_identifier_is_never_published_as_somewhere_to_go() -> None:
    """It is a name. Every URL in the record is a place, and the two were conflated
    twice — in the fallback host and in `landingPage`. No value in a rendered record
    may be the identifier, apart from `@id` itself."""
    for resource in (dataset(), service()):
        record = render(resource, node_id="alpha-onprem", tenant_slug="alpha", base_url=GATEWAY)
        uri = record.pop("@id")

        assert uri not in str(record)


def test_the_data_url_is_on_the_address_the_node_is_published_at() -> None:
    record = render(dataset(), node_id="alpha-onprem", tenant_slug="alpha", base_url=GATEWAY)
    url = record["dcat:distribution"][0]["dcat:accessURL"]

    assert url.startswith(GATEWAY)
    # The fault this replaces: the identifier's host, which nothing answers on.
    assert "alpha-onprem.circuless.bavenir.eu" not in url


def test_the_endpoint_url_is_too() -> None:
    record = render(service(), node_id="alpha-onprem", tenant_slug="beta", base_url=GATEWAY)
    assert record["dcat:endpointURL"].startswith(GATEWAY)


def test_a_service_still_never_publishes_its_upstream() -> None:
    """Unchanged, and the reason the endpoint is the node's at all: a consumer with the
    upstream address goes round `decide()`, the agreement check and the log."""
    record = render(service(), node_id="alpha-onprem", tenant_slug="beta", base_url=GATEWAY)
    assert "upstream.internal" not in str(record)


# --- the published path is one the node serves -------------------------------------------


def wildcarded(path: str) -> str:
    """`/v1/t/alpha/resources/<uuid>/data` against `/v1/t/{tenant_slug}/.../data`."""
    parts = path.strip("/").split("/")
    return "/" + "/".join(
        "*" if part in {"alpha", "beta", str(RESOURCE)} else part for part in parts
    )


@pytest.fixture
def served(settings: Settings) -> set[str]:
    return {
        "/"
        + "/".join("*" if part.startswith("{") else part for part in route.strip("/").split("/"))
        for route in route_paths(create_public_app(settings))
    }


def test_the_data_url_points_at_a_route_this_node_serves(served: set[str]) -> None:
    """The fault this replaces: the path omitted `/v1`, which every route carries
    (invariant 1). `endswith("/data")` held throughout."""
    record = render(dataset(), node_id="n", tenant_slug="alpha", base_url=GATEWAY)
    path = urlparse(record["dcat:distribution"][0]["dcat:accessURL"]).path

    assert path.startswith("/v1/")
    assert wildcarded(path) in served, f"{path} is not a route this node serves"


def test_the_endpoint_url_points_at_one_too(served: set[str]) -> None:
    record = render(service(), node_id="n", tenant_slug="beta", base_url=GATEWAY)
    path = urlparse(record["dcat:endpointURL"]).path

    # `/invoke` is served as `/invoke/{upstream_path:path}`, so the base is a prefix of
    # a real route rather than one itself.
    assert path.startswith("/v1/")
    assert any(route.startswith(wildcarded(path)) for route in served), path


# --- the JSON-LD context declares what the record uses -----------------------------------


def prefixes_used(node: object) -> set[str]:
    """Every `prefix:term` the record spells, as a key or as an `@type`."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "@context":
                continue
            if ":" in key and not key.startswith("@"):
                found.add(key.split(":", 1)[0])
            if key == "@type" and isinstance(value, str) and ":" in value:
                found.add(value.split(":", 1)[0])
            found |= prefixes_used(value)
    elif isinstance(node, list):
        for item in node:
            found |= prefixes_used(item)
    return found


def test_the_context_declares_every_prefix_the_record_uses() -> None:
    """The fault this replaces: `@context` declared `dcat` and `dct` while the record
    emitted `circuless:discoverability`, `circuless:classification` and
    `circuless:shape`.

    An undeclared prefix is not a tolerated extension. A JSON-LD processor resolves it
    against nothing and **drops the term**, so those three reached no consumer that
    parsed the record rather than string-matching it — and nothing failed, which is why
    it survived. Written against the record rather than against a list of prefixes, so a
    fourth term added later is covered without anyone remembering this test."""
    for resource in (dataset(), service()):
        record = render(resource, node_id="n", tenant_slug="alpha", base_url=GATEWAY)
        declared = set(record["@context"])
        used = prefixes_used(record)

        # Vacuity guard: a record using no prefixed names at all would satisfy the
        # subset assertion below and prove nothing. This is the one that regressed.
        assert "circuless" in used, "no project term in the record — the test is vacuous"
        assert "dcat" in used
        assert used <= declared, f"undeclared prefixes: {sorted(used - declared)}"


def test_the_term_namespace_is_not_the_concept_namespace() -> None:
    """`TERM_BASE` names properties; `THEME_BASE` names four value-chain phases.
    Collapsing them would leave `circuless:value-chain` ambiguous between a property and
    a phase, which is the distinction DCAT itself keeps."""
    assert TERM_BASE.startswith("https://")
    # A prefix has to end in a delimiter or `circuless:classification` concatenates into
    # a different IRI than anyone intended.
    assert TERM_BASE.endswith(("#", "/"))
    assert not TERM_BASE.startswith(THEME_BASE)


# --- the API contract --------------------------------------------------------------------


def test_a_service_describes_its_contract_through_the_node() -> None:
    """With no `openapi_ref`, the description is the node's own `/invoke/openapi.json`.

    Through the node, not at the upstream: the contract is reached exactly as the
    service is, under `decide()` and in the log. A service must serve the document at
    `openapi.json` relative to the endpoint it registered, which is what lets the node
    compose this without asking."""
    record = render(service(), node_id="n", tenant_slug="beta", base_url=GATEWAY)

    assert record["dcat:endpointDescription"] == openapi_url(GATEWAY, "beta", RESOURCE)
    assert record["dcat:endpointDescription"] == f"{record['dcat:endpointURL']}/{OPENAPI_DOCUMENT}"


def test_the_providers_own_document_wins_when_they_have_one() -> None:
    """A provider who publishes a fetchable contract keeps it: theirs is reachable
    without an agreement, and the node's is not."""
    resource = service()
    resource.openapi_ref = "https://beta.example/openapi.json"
    record = render(resource, node_id="n", tenant_slug="beta", base_url=GATEWAY)

    assert record["dcat:endpointDescription"] == "https://beta.example/openapi.json"


def test_the_contract_url_is_absolute() -> None:
    """DCAT-AP gives `endpointDescription` the range `rdfs:Resource` — an IRI. A path
    relative to the endpoint would have been easier to store and is not conformant."""
    record = render(service(), node_id="n", tenant_slug="beta", base_url=GATEWAY)
    parsed = urlparse(record["dcat:endpointDescription"])

    assert parsed.scheme and parsed.netloc


def test_the_contract_url_points_at_a_route_this_node_serves(served: set[str]) -> None:
    """The same check the other two URLs get. `/invoke/{upstream_path:path}` swallows
    `openapi.json`, so this asserts the prefix is a real route rather than that the
    whole path is one."""
    record = render(service(), node_id="n", tenant_slug="beta", base_url=GATEWAY)
    path = urlparse(record["dcat:endpointDescription"]).path

    assert path.startswith("/v1/")
    assert path.endswith(f"/invoke/{OPENAPI_DOCUMENT}")
    invoke_base = wildcarded(path).rsplit("/", 1)[0]
    assert any(route.startswith(invoke_base) for route in served), path


def test_the_document_name_is_the_one_services_are_obliged_to_serve() -> None:
    """Pinned as a literal, deliberately, and not against the constant.

    Every other assertion in this section spells `OPENAPI_DOCUMENT`, so renaming its
    value moves both sides of the comparison together and they all keep passing — a
    mutation to `swagger.json` survived the first run of this suite for exactly that
    reason. The value is a contract with every partner: it is written into the
    integration requirements and each registered service serves its document at that
    name, so it cannot move here without their services moving first."""
    assert OPENAPI_DOCUMENT == "openapi.json"

    record = render(service(), node_id="n", tenant_slug="beta", base_url=GATEWAY)
    assert record["dcat:endpointDescription"].endswith("/invoke/openapi.json")


def test_a_dataset_describes_no_endpoint() -> None:
    """`endpointDescription` belongs to a `dcat:DataService`. A Dataset carrying one
    would be invalid, and the composition runs in the service branch only."""
    record = render(dataset(), node_id="n", tenant_slug="alpha", base_url=GATEWAY)

    assert "dcat:endpointDescription" not in record


# --- a node that is not published yet ------------------------------------------------------


def test_a_dataset_publishes_no_distribution_when_the_node_has_no_address() -> None:
    """It used to fall back to the identifier's host, so the record carried an
    `accessURL` a browser could not follow — a wrong answer dressed as a real one.
    DCAT-AP makes `accessURL` mandatory on a Distribution, so the honest record has no
    Distribution: this node has not told anyone where it is."""
    record = render(dataset(), node_id="alpha-onprem", tenant_slug="alpha")

    assert "dcat:distribution" not in record
    assert "alpha-onprem.circuless.bavenir.eu" not in str(record)


def test_a_service_publishes_no_endpoint_either() -> None:
    record = render(service(), node_id="alpha-onprem", tenant_slug="alpha")

    assert "dcat:endpointURL" not in record
    assert "dcat:landingPage" not in record


def test_a_service_describes_no_contract_without_an_address_either() -> None:
    """Nothing to compose from. The honest record omits the key, exactly as it omits
    `endpointURL` — a published IRI that resolves nowhere is worse than none, which is
    the whole lesson of this file."""
    record = render(service(), node_id="alpha-onprem", tenant_slug="alpha")

    assert "dcat:endpointDescription" not in record
    assert "dcat:endpointURL" not in record


def test_what_does_not_depend_on_the_address_survives() -> None:
    """The record is still a record: the parts that are the provider's rather than the
    deployment's are unaffected, so this is an incomplete catalogue entry and not an
    empty one."""
    resource = service()
    resource.openapi_ref = "https://beta.example/openapi.json"
    record = render(resource, node_id="alpha-onprem", tenant_slug="alpha")

    assert record["@id"] == resource_uri("alpha-onprem", "alpha", RESOURCE)
    assert record["dct:title"] == "Recycled PET batch 7"
    assert record["dct:license"]
    assert record["dcat:endpointDescription"] == "https://beta.example/openapi.json"


# --- landingPage is not published ----------------------------------------------------------


def test_no_record_carries_a_landing_page() -> None:
    """R15 lists `landingPage` on a `dcat:DataService`, and this node does not publish it.

    A landing page is one a reader can navigate to. This node has none to offer a
    consumer: the only per-resource page it serves is in the admin UI under `/ui`, and
    the Cloud gateway routes `/v1` and nothing else (C3, invariant 17), so the published
    address cannot reach it — nor could a consumer from another organisation use it.

    The field has been wrong twice, each time by pointing somewhere a reader could not
    go: first the `@id`, which is a name and not a place, then that `/ui` page. Absent
    says what is true.
    """
    for resource in (dataset(), service()):
        published = render(resource, node_id="alpha-onprem", tenant_slug="beta", base_url=GATEWAY)
        unpublished = render(resource, node_id="alpha-onprem", tenant_slug="beta")

        assert "dcat:landingPage" not in published
        assert "dcat:landingPage" not in unpublished


def test_nothing_in_a_record_points_at_the_ui_mount(settings: Settings) -> None:
    """The stronger form, and the one that survives somebody reintroducing the field
    under another name. `/ui` is invariant 2's single anonymous exemption and the
    gateway does not route it, so no published value may lead there.

    Asserted against the mount the app really has, so that renaming `/ui` cannot leave
    this passing against a path nothing serves.
    """
    from .harness.routes import mount_paths

    [mount] = mount_paths(create_public_app(settings))
    record = render(service(), node_id="alpha-onprem", tenant_slug="beta", base_url=GATEWAY)

    assert mount == "/ui"
    assert f"{mount}/" not in str(record)


def test_a_service_still_publishes_what_it_can_offer() -> None:
    """Removing one field is not removing the shape. R15's point was that a service is
    a `dcat:DataService` rather than a Dataset with a URL in it, and it still is."""
    resource = service()
    resource.openapi_ref = "https://beta.example/openapi.json"
    record = render(resource, node_id="alpha-onprem", tenant_slug="beta", base_url=GATEWAY)

    assert record["@type"] == "dcat:DataService"
    assert record["dcat:endpointURL"].startswith(GATEWAY)
    assert record["dcat:endpointDescription"] == "https://beta.example/openapi.json"


def test_a_trailing_slash_on_the_address_does_not_double_up() -> None:
    assert "//v1" not in access_url(f"{GATEWAY}/", "alpha", RESOURCE)
    assert "//v1" not in invoke_url(f"{GATEWAY}/", "alpha", RESOURCE)
