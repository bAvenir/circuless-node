---
type: C4 Component
title: Resource registry
status: stable
groma:
  id: resource-registry
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/resources.py
    - scanner: python
      file: src/circuless_node/management.py
    - scanner: python
      file: src/circuless_node/vocabularies.py
  group: Resources and transfer
description: What a tenant has published, who may change it, and the controlled vocabularies it must use.
---

Datasets and services are registered here, with the rules about who may do so: an organisation's admins or its service principals may register, update, upload and withdraw; **only admins** may set an upstream credential; admins and the platform operator may read the log.

Two guards sit on registration. A resource is created **hidden and org-visible** unless something says otherwise, so nothing is published by forgetting. And a licence from a controlled list is required before anything reaches the catalogue.

Every resource carries a classification, and a Bavenir-operated node **refuses** a sensitive one — not because this node is less secure, but because Bavenir holding a partner's sensitive data is an arrangement the project undertook not to make. The refusal defaults closed: a node nobody configured is treated as Bavenir-operated.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [src/circuless_node/resources.py](../../../../../../src/circuless_node/resources.py) | [src/circuless_node/decide.py](../../../../../../src/circuless_node/decide.py) | Authorizes the change | Python call |
| [src/circuless_node/resources.py](../../../../../../src/circuless_node/resources.py) | [src/circuless_node/dcat.py](../../../../../../src/circuless_node/dcat.py) | Renders the catalogue record | Python call |
