"""Rendering a resource as DCAT-AP (N5, F3, F4).

The node composes the JSON-LD from typed columns rather than storing whatever a provider
sent. That choice is what makes the licence rule enforceable and the catalogue
searchable: "has a licence from the controlled list" is a column check here, and walking
arbitrary provider JSON-LD there.

Two shapes, and the distinction is a review finding (R15):

* a **dataset** is a `dcat:Dataset` with a `dcat:Distribution` carrying `accessURL`;
* a **service** is a `dcat:DataService` with `endpointURL` and `endpointDescription` —
  *not* a Dataset with a URL in it, which is what an earlier draft had.

`endpointDescription` has two sources and the provider's wins: `openapi_ref` if they
registered one, otherwise the node's own `/invoke/openapi.json`. The second is why a
service must serve its contract at `openapi.json` relative to its registered endpoint —
without that obligation the node would have to guess, and a published IRI that 404s is
worse than an absent one.

`dcat:landingPage` is **not published**, although R15 lists it. DCAT says a landing page
is one you can navigate to, and this node has none to offer a consumer: its only
per-resource page is in the admin UI under `/ui`, which the Cloud gateway does not route
(it publishes `/v1` and nothing else, invariant 17) and which a consumer from another
organisation could not use anyway. The field was briefly the `@id` and then that page;
both were somewhere a reader could not go. Omitting it says what is true.

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

#: The namespace for the project's own **terms** — the properties DCAT-AP has no word
#: for. `circuless:classification` resolves to `…/ns#classification`.
#:
#: **Separate from `THEME_BASE` deliberately.** That is a concept scheme: its IRIs name
#: four value-chain phases. This one names properties. DCAT draws the same line — terms
#: under `dcat#`, code lists elsewhere — and merging them would leave
#: `circuless:value-chain` ambiguous between a property and a phase.
#:
#: **Do not move it once records are published.** A consumer that stored a record
#: resolved these terms against this IRI; changing it renames every property they kept.
TERM_BASE = "https://circuless.bavenir.eu/ns#"

#: The namespace for resource identifiers. The same project namespace as `THEME_BASE`,
#: and for the same reason: these are names the project mints, not addresses anything
#: serves. Nothing listens here and nothing is meant to.
IDENTIFIER_BASE = "https://circuless.bavenir.eu/id"


def resource_uri(node_id: str, tenant_slug: str, resource_id) -> str:  # noqa: ANN001
    """The stable identifier for a resource across the platform.

    Built from the node and tenant rather than being a bare UUID, so that a record lifted
    out of the catalogue still says where it came from.

    **An identifier, not an address.** It is deterministic and depends on nothing a
    deployment configures, which is exactly what an `@id` should be — and exactly what a
    URL somebody fetches should not be. `resource_path` below is what goes on the wire.

    It used to be built as `https://{node_id}.circuless.bavenir.eu/...`, which looked
    like a node's address, was never any node's address, and resolved nowhere: nodes are
    published under `*.nodes.circuless.bavenir.eu`. Two bugs came out of that shape
    before this one — an `accessURL` on the wrong host, then the same host again as a
    fallback — because a name that is spelled like an address eventually gets used as
    one. It is now spelled so it cannot be: one namespace the project owns, with the
    node as a path segment rather than a hostname, so a node moving host leaves every
    identifier it ever minted unchanged.

    The resource UUID stays the last segment: the Cloud recovers it from there
    (`catalogue.parse_record`), because C6's agreements name a resource, not a record.
    """
    return f"{IDENTIFIER_BASE}/nodes/{node_id}/tenants/{tenant_slug}/resources/{resource_id}"


def resource_path(tenant_slug: str, resource_id) -> str:  # noqa: ANN001
    """Where this resource lives on whatever address the node answers."""
    return f"/v1/t/{tenant_slug}/resources/{resource_id}"


def access_url(base_url: str, tenant_slug: str, resource_id) -> str:  # noqa: ANN001
    """Where a consumer fetches the bytes.

    On `base_url` — the node's own public address — and not on the identifier's host.
    DCAT-AP says a distribution's `accessURL` is where you get the data; one that is
    not fetchable is simply wrong, and this one was: it was built from the identifier,
    whose host was then a fixed `{node_id}.circuless.bavenir.eu` pattern, while a node
    is published at whatever `gateway_base_url` says — `…nodes.circuless.bavenir.eu` in
    the beta. A browser following the record went to a host nothing answered on. The
    identifier no longer has a host anything could be tempted to fetch; see
    `resource_uri`.
    """
    return f"{base_url.rstrip('/')}{resource_path(tenant_slug, resource_id)}/data"


def invoke_url(base_url: str, tenant_slug: str, resource_id) -> str:  # noqa: ANN001
    """Where a consumer calls the service — the node's `/invoke`, never upstream."""
    return f"{base_url.rstrip('/')}{resource_path(tenant_slug, resource_id)}/invoke"


#: Where a service serves its own OpenAPI document, **relative to the `endpoint_url` it
#: registered**. An obligation on every registered service rather than a convention, so
#: that the node can compose the description without asking — see the integration
#: requirements. The DPP adapter already satisfies it: it registers `…:8000/v1` and
#: serves `/v1/openapi.json`, so the document sits at `openapi.json` relative to its
#: endpoint. A fixed `/api/v1/openapi.json` would not have, which is why the rule is
#: relative to the registration and not to the service's root.
OPENAPI_DOCUMENT = "openapi.json"


def openapi_url(base_url: str, tenant_slug: str, resource_id) -> str:  # noqa: ANN001
    """Where a consumer fetches the service's API contract, through the node.

    `endpointURL` plus the document, so the contract is reached exactly as the service
    is: through `/invoke`, under `decide()`, with the upstream credential injected and
    the call logged. A provider whose document is publicly fetchable overrides this with
    `openapi_ref`; most cannot, because the document sits behind the same network the
    service does.
    """
    return f"{invoke_url(base_url, tenant_slug, resource_id)}/{OPENAPI_DOCUMENT}"


def render(
    resource: Resource, *, node_id: str, tenant_slug: str, base_url: str | None = None
) -> dict[str, Any]:
    """One DCAT-AP record. Pure — no database, and no settings beyond what is passed in.

    `base_url` is this node's public address, as a consumer's browser would reach it
    (`gateway_base_url`, or the overlay address for a client that can join it). The
    identifier is a name and never moves; every fetchable URL hangs off `base_url`.

    **Without it, the record carries no URLs at all.** It used to fall back to the
    identifier's host, which resolves nowhere, so a node with no configured address
    published a record whose `accessURL` a browser could not follow — a wrong answer
    dressed as a real one. A record that omits the URL says what is true: this node has
    not told anyone where it is. `sync` passes the gateway address, or the overlay one,
    and a node with neither is reachable by nobody anyway.
    """
    uri = resource_uri(node_id, tenant_slug, resource.id)
    reachable_at = base_url.rstrip("/") if base_url else None
    record: dict[str, Any] = {
        "@context": {
            # Until 18 Nov 2026 this declared `dcat` and `dct` only, while the record
            # emitted `circuless:discoverability`, `circuless:classification` and
            # `circuless:shape`. An undeclared prefix is not a tolerated extension: a
            # JSON-LD processor resolves it against nothing and **drops the term**. All
            # three were invisible to every consumer that parsed the record rather than
            # string-matching it.
            "circuless": TERM_BASE,
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
        # No `base_url`, no distribution. DCAT-AP makes `accessURL` mandatory on a
        # Distribution, so a distribution without one would be invalid, and a
        # distribution with a made-up one is worse.
        if reachable_at:
            record["dcat:distribution"] = [
                {
                    "@type": "dcat:Distribution",
                    # The node's own path. Fetching it is decided by `decide()`.
                    "dcat:accessURL": access_url(reachable_at, tenant_slug, resource.id),
                    "circuless:shape": resource.shape.value,
                }
            ]
    else:
        if reachable_at:
            # The node's /invoke path, never `resource.endpoint_url` — see the module
            # docstring.
            record["dcat:endpointURL"] = invoke_url(reachable_at, tenant_slug, resource.id)
        # DCAT-AP: an IRI to a machine-readable description of the operations and
        # parameters. Its range is `rdfs:Resource`, so it must be absolute and
        # dereferenceable — which rules out a path relative to the endpoint, however
        # convenient that would have been to store.
        #
        # The provider's own document wins when they have published one somewhere a
        # consumer can reach. Otherwise the node's own, which every service can answer
        # because serving `openapi.json` at its registered endpoint is an obligation.
        # With no address there is no absolute IRI to offer, and the key is omitted for
        # the same reason `endpointURL` is: a record that says nothing beats one that
        # sends a reader somewhere nothing answers.
        description = resource.openapi_ref or (
            openapi_url(reachable_at, tenant_slug, resource.id) if reachable_at else None
        )
        if description:
            record["dcat:endpointDescription"] = description

    return record


def _type_of(resource: Resource) -> str:
    return "dcat:Dataset" if resource.kind is ResourceKind.DATASET else "dcat:DataService"
