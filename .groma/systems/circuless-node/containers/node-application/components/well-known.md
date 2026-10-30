---
type: C4 Component
title: Node discovery document
status: stable
groma:
  id: well-known
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/well_known.py
  group: Cloud and overlay
description: What this node says about itself to the platform, behind a token.
---

Serves `/.well-known/circuless-node`, the one route outside `/v1`. It is not anonymous: a platform token is required, because an unauthenticated description of a node is an invitation to probe it.
