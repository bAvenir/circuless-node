"""Where a published record tells a consumer to go.

Two faults lived here undetected because the only assertions were
`accessURL.endswith("/data")` and `endpointURL.endswith("/invoke")`, which hold
whatever the host is and whether or not the path is one this node serves.

**The host was wrong.** Both URLs were built from `resource_uri`, whose host is a fixed
`{node_id}.circuless.bavenir.eu` pattern that comes from nothing configurable. A node is
published at `gateway_base_url` — `…nodes.circuless.eu` in the beta — so a browser
following the record went somewhere nothing answered.

**The path was wrong.** It omitted `/v1`, which every route on the node carries
(invariant 1). Even on the right host it would have been a 404.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

import pytest

from circuless_node.app import create_public_app
from circuless_node.dcat import access_url, invoke_url, render, resource_uri
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
GATEWAY = "https://alpha.nodes.circuless.eu"


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
    assert "circuless.bavenir.eu" in record["@id"]


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


def test_it_falls_back_to_the_identifier_host_when_no_address_is_configured() -> None:
    """Nothing can reach such a node anyway, so the record is no less useful than it
    was. A node that *is* published must pass its address."""
    record = render(dataset(), node_id="alpha-onprem", tenant_slug="alpha")
    url = record["dcat:distribution"][0]["dcat:accessURL"]

    assert url.startswith("https://alpha-onprem.circuless.bavenir.eu")
    assert url.startswith(record["@id"].rsplit("/t/", 1)[0])


def test_a_trailing_slash_on_the_address_does_not_double_up() -> None:
    assert "//v1" not in access_url(f"{GATEWAY}/", "alpha", RESOURCE)
    assert "//v1" not in invoke_url(f"{GATEWAY}/", "alpha", RESOURCE)
