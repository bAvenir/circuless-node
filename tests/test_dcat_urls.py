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

A third followed from the same root, and is fixed here: the identifier was *spelled*
like an address, so it kept getting used as one — as the fallback `accessURL` host, and
as `dcat:landingPage` outright. It is now minted under one project namespace the project
owns, with the node as a path segment and not a hostname, which is a shape nobody can
mistake for somewhere to GET.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

import pytest

from circuless_node.app import create_public_app
from circuless_node.dcat import (
    IDENTIFIER_BASE,
    access_url,
    invoke_url,
    landing_page,
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


# --- landingPage is a page you can land on ------------------------------------------------


def test_the_landing_page_is_the_nodes_own_page_for_the_resource() -> None:
    """DCAT says `landingPage` is navigable. It used to be the `@id`, which is neither
    a page nor navigable. The node's admin UI has a page per resource under `/ui`."""
    record = render(service(), node_id="alpha-onprem", tenant_slug="beta", base_url=GATEWAY)
    page = record["dcat:landingPage"]

    assert page == landing_page(GATEWAY, "beta", RESOURCE)
    assert page.startswith(f"{GATEWAY}/ui/")
    assert str(RESOURCE) in page


def test_the_landing_page_is_on_a_mount_the_node_actually_serves(settings: Settings) -> None:
    """`/ui` is the one anonymous prefix (invariant 2's single exemption). If it were
    renamed or dropped, this URL would point at nothing — and the fragment after it is
    the UI's own routing, which the server never sees."""
    from circuless_node.app import ANONYMOUS_PREFIXES

    from .harness.routes import mount_paths

    page = landing_page(GATEWAY, "beta", RESOURCE)
    path = urlparse(page).path

    assert path.startswith("/ui/")
    assert "/ui" in ANONYMOUS_PREFIXES
    assert "/ui" in mount_paths(create_public_app(settings))


def test_a_trailing_slash_on_the_address_does_not_double_up() -> None:
    assert "//v1" not in access_url(f"{GATEWAY}/", "alpha", RESOURCE)
    assert "//v1" not in invoke_url(f"{GATEWAY}/", "alpha", RESOURCE)
