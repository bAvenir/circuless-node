---
type: C4 Component
title: Tenancy
status: stable
groma:
  id: tenancy
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/tenancy.py
    - scanner: python
      file: src/circuless_node/tenants.py
  group: Identity and authorisation
description: Keeps several organisations in one process from seeing each other's rows.
---

Tenant-owned tables carry a tenant id and are filtered **once, centrally**, rather than in each handler — a hand-written filter is a thing somebody eventually forgets.

Three tables are deliberately outside that scope because they are node-global rather than tenant-owned: the agreement cache, the organisation map, and the node's own identity.
