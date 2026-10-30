---
type: C4 Actor
title: Node operator
status: stable
groma:
  id: node-operator
---

Runs the host this node is installed on: a partner on their own premises, or Bavenir for a node it hosts. Installs the container, holds the node's private key, applies updates, and backs up the key material the platform deliberately never backs up for them.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [node-operator](node-operator.md) | [src/circuless_node/__main__.py](../../src/circuless_node/__main__.py) | Runs the node | Container |
