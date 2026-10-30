---
type: C4 Component
title: Purge job
status: stable
groma:
  id: purge
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/purge.py
  group: Runtime
description: 'Stage two of a two-stage deletion: removing what a withdrawal scheduled.'
---

Withdrawing a resource takes effect immediately — the decision function refuses it and the catalogue record is withdrawn. This removes the data and the metadata afterwards, once the retention period has passed.

**Access log entries stay.** The record that somebody read something is not deleted because the something was.
