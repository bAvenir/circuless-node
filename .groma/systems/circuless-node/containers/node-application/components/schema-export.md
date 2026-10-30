---
type: C4 Component
title: OpenAPI export
status: stable
groma:
  id: schema-export
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/openapi.py
  group: Runtime
description: Writes the committed description of the node's API.
---

The public application serves no schema, so the document is produced by running the application and asking it, and the result is committed. The documentation site renders the platform's API surface from it, and CI fails if the committed copy has drifted.

It describes the **public** application only. Publishing the internal one would invite somebody to try those paths through the gateway.
