---
type: C4 Component
title: Overlay address
status: stable
groma:
  id: overlay
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/overlay.py
  group: Cloud and overlay
  technology: NetBird, WireGuard
description: Works out the address other parts of the platform should use to reach this node.
---

A node joins the encrypted overlay by connecting outwards, and then has an address on it that is not the one its host knows. The gateway's route to this node is generated from that address, so getting it wrong produces a route that proxies to itself or to nothing.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [src/circuless_node/overlay.py](../../../../../../src/circuless_node/overlay.py) | [netbird-overlay](../../../../../externals/netbird-overlay.md) | Finds its overlay address | WireGuard |
