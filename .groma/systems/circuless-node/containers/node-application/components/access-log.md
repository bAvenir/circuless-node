---
type: C4 Component
title: Access log
status: stable
groma:
  id: access-log
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/access_log.py
  group: Identity and authorisation
description: Every decision this node made, allow and deny, append-only.
---

One entry per decision — consumption and management alike — recording the request id, tenant, resource, action, the pseudonymous subject, the principal type, the acting client, the acting organisation, the decision, the reason code and the bytes transferred.

**Append-only in the database, not merely by convention**: a trigger refuses any update except setting the byte count once, and refuses deletes outright. A transfer's size is unknown until it has streamed, and the entry must exist before the stream starts or a dropped connection would leave no record that access was granted at all.

It never stores a name or an email. Node-audienced tokens carry neither, so there is nothing to store.
