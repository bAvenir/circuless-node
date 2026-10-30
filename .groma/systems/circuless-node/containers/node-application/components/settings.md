---
type: C4 Component
title: Settings
status: stable
groma:
  id: settings
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/settings.py
  group: Runtime
  technology: pydantic-settings
description: Every value the node needs, from the environment or a .env file.
---

Holds the node's identity, the issuer, the Cloud's address, the two socket bindings, the CORS allowlist and who operates the node.

Two of them are security decisions rather than configuration. The CORS allowlist refuses `*` at startup rather than at review, because browser consumption carries a bearer token and a wildcard would let any site spend it. And the operator setting **defaults to Bavenir**, so a node nobody configured refuses sensitive data — the permissive value is the one that has to be typed out.
