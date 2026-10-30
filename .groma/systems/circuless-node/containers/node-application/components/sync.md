---
type: C4 Component
title: Cloud synchronisation
status: stable
groma:
  id: sync
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/sync.py
  group: Cloud and overlay
description: Pushes what this node publishes and pulls the agreements it enforces, every thirty seconds.
---

One pass does three things: push a DCAT record per tenant when its resources have changed, pull the provider-side agreements and the organisation map, and say hello so the Cloud knows the node is alive.

The pull is what makes revocation take effect. Thirty seconds is therefore the headline number in the platform's promise about withdrawing access, plus whatever is left of an access token's five minutes.

**If the Cloud is unreachable the node keeps deciding** from its cache rather than failing open or closed at random, and reports that it is stale on its health and metrics endpoints. It also adopts the tenants the feed names — creating them, never deleting or guessing.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [src/circuless_node/sync.py](../../../../../../src/circuless_node/sync.py) | [circuless-cloud](../../../../../externals/circuless-cloud.md) | Publishes records, pulls agreements | HTTPS, REST |
| [src/circuless_node/sync.py](../../../../../../src/circuless_node/sync.py) | [src/circuless_node/db.py](../../../../../../src/circuless_node/db.py) | Caches agreements | SQLModel |
| [src/circuless_node/sync.py](../../../../../../src/circuless_node/sync.py) | [src/circuless_node/dcat.py](../../../../../../src/circuless_node/dcat.py) | Publishes the record | Python call |
