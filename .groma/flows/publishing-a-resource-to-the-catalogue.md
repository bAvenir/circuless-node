---
type: Groma Flow
title: Publishing a resource to the catalogue
groma:
  id: publishing-a-resource-to-the-catalogue
---

How anything gets into the platform, and the only path that reaches the Cloud outwards. Everything else the node does is inbound.

Two guards sit on registration, and both default closed. A resource is created hidden and org-visible unless something says otherwise, so nothing is published by forgetting to say it should not be. And a licence from a controlled list is required before anything reaches the catalogue, because a record a consumer cannot act on wastes their time and the provider.

A third check is about where the node runs rather than what is being published. Every resource carries a classification, and a Bavenir-operated node refuses a sensitive one — not because this node is less secure, but because Bavenir holding a partner sensitive data is an arrangement the project undertook not to make. That refusal defaults closed too: a node nobody configured is treated as Bavenir-operated.

What leaves the node is metadata, never data. The distribution in the DCAT record points at the node own address, and a service upstream URL is shown only to someone who may manage the resource.

## Steps

| From | To | Action |
| --- | --- | --- |
| [Organisation administrator](../actors/organisation-administrator.md) | [Resource registry](../systems/circuless-node/containers/node-application/components/resource-registry.md) | Registers a dataset or a service in their own tenant |
| [Resource registry](../systems/circuless-node/containers/node-application/components/resource-registry.md) | [Access decision](../systems/circuless-node/containers/node-application/components/access-decision.md) | Checks the caller may change this organisation resources — an admin or a service principal, and only for registering; the upstream credential is admins only |
| [Resource registry](../systems/circuless-node/containers/node-application/components/resource-registry.md) | [DCAT record](../systems/circuless-node/containers/node-application/components/dcat.md) | Renders the record once the licence and the classification pass: a dataset as dcat:Dataset, a service as dcat:DataService |
| [Cloud synchronisation](../systems/circuless-node/containers/node-application/components/sync.md) | [DCAT record](../systems/circuless-node/containers/node-application/components/dcat.md) | On the next pass, collects a record per tenant whose resources have changed |
| [Cloud synchronisation](../systems/circuless-node/containers/node-application/components/sync.md) | [CIRCULess Cloud](../externals/circuless-cloud.md) | Pushes the metadata to the catalogue, where another organisation can find it. The data itself never leaves |
