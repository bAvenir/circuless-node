---
type: C4 Component
title: Command line
status: stable
groma:
  id: cli
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/__main__.py
  group: Runtime
description: Starting the node, and the operator commands that go with it.
---

Brings up both applications, and carries the operator-facing commands a node host needs. Distributed as a pinned `uvx` install and as a non-root container image pinned by digest.
