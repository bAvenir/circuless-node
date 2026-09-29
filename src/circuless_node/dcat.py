"""Rendering a resource as DCAT-AP (N5, F3, F4).

The node composes the JSON-LD from typed columns rather than storing whatever a provider
sent. That choice is what makes the licence rule enforceable and the catalogue
searchable: "has a licence from the controlled list" is a column check here, and walking
arbitrary provider JSON-LD there.

Two shapes, and the distinction is a review finding (R15):

* a **dataset** is a `dcat:Dataset` with a `dcat:Distribution` carrying `accessURL`;
* a **service** is a `dcat:DataService` with `endpointURL`, `endpointDescription` and
  `landingPage` — *not* a Dataset with a URL in it, which is what an earlier draft had.

Every record carries `dct:license` and a `dcat:theme` from the value-chain vocabulary.

## What never appears here

**The service's real endpoint.** `endpoint_url` is where *this node* proxies to; a
consumer reaches the service only through the node, and publishing the upstream address
would let them go round it — past `decide()`, past the agreement check, past the log.
`endpointURL` in the record is the node's own `/invoke` path.

**Anything about who may read it.** `visibility` is an access rule, not metadata, and the
catalogue is filtered by `discoverability` and never by `visibility` (invariant 7 on the
Cloud side). Putting it in the record would publish the access policy of every resource
to everyone who can search.
"""

from __future__ import annotations

from typing import Any

from .models import Resource
from .vocabularies import LICENCES, ResourceKind

#: The vocabulary namespace for `dcat:theme`. The four value-chain phases are a project
#: vocabulary, so they get a project namespace rather than being bent into an EU one.
THEME_BASE = "https://circuless.bavenir.eu/vocabulary/value-chain-phase"


def resource_uri(node_id: str, tenant_slug: str, resource_id) -> str:  # noqa: ANN001
    """The stable identifier for a resource across the platform.

    Built from the node and tenant rather than being a bare UUID, so that a record lifted
    out of the catalogue still says where it came from.
    """
    return f"https://{node_id}.circuless.bavenir.eu/t/{tenant_slug}/resources/{resource_id}"


def access_url(node_id: str, tenant_slug: str, resource_id) -> str:  # noqa: ANN001
    return f"{resource_uri(node_id, tenant_slug, resource_id)}/data"


def invoke_url(node_id: str, tenant_slug: str, resource_id) -> str:  # noqa: ANN001
    return f"{resource_uri(node_id, tenant_slug, resource_id)}/invoke"


def render(resource: Resource, *, node_id: str, tenant_slug: str) -> dict[str, Any]:
    """One DCAT-AP record. Pure — no database, no settings beyond the two identifiers."""
    uri = resource_uri(node_id, tenant_slug, resource.id)
    record: dict[str, Any] = {
        "@context": {
            "dcat": "http://www.w3.org/ns/dcat#",
            "dct": "http://purl.org/dc/terms/",
        },
        "@id": uri,
        "@type": _type_of(resource),
        "dct:title": resource.title,
        "dct:description": resource.description,
        "dcat:theme": f"{THEME_BASE}/{resource.theme.value}",
        # Null only while hidden; publishing requires it (NFR9), so a record that reaches
        # the catalogue always has one.
        "dct:license": LICENCES.get(resource.licence) if resource.licence else None,
        "dct:issued": resource.created_at.isoformat(),
        "dct:modified": resource.updated_at.isoformat(),
        # Not an access rule — it is why this record is in the catalogue at all, and the
        # Cloud filters on it (invariant 7).
        "circuless:discoverability": resource.discoverability.value,
        "circuless:classification": resource.classification.value,
    }

    if resource.kind is ResourceKind.DATASET:
        record["dcat:distribution"] = [
            {
                "@type": "dcat:Distribution",
                # The node's own path. Fetching it is decided by `decide()`.
                "dcat:accessURL": access_url(node_id, tenant_slug, resource.id),
                "circuless:shape": resource.shape.value,
            }
        ]
    else:
        # The node's /invoke path, never `resource.endpoint_url` — see the module docstring.
        record["dcat:endpointURL"] = invoke_url(node_id, tenant_slug, resource.id)
        if resource.openapi_ref:
            record["dcat:endpointDescription"] = resource.openapi_ref
        record["dcat:landingPage"] = uri

    return record


def _type_of(resource: Resource) -> str:
    return "dcat:Dataset" if resource.kind is ResourceKind.DATASET else "dcat:DataService"
