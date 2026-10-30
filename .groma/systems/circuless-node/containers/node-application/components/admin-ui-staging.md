---
type: C4 Component
title: Admin UI staging
status: stable
groma:
  id: admin-ui-staging
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/ui_staging.py
  group: Runtime
description: Copies the shipped browser UI into place and writes the four-key config it reads.
---

At startup the static UI is copied out of the package and a `config.json` is written beside it from the node's own settings: issuer, public client id, node id, scope.

Exactly four keys, asserted here and refused at startup if widened — because that file is readable without a token, and the cost of an extra key is a disclosure nobody reviewed.
