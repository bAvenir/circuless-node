---
type: C4 Actor
title: Organisation administrator
status: stable
groma:
  id: organisation-administrator
---

Administers one organisation's tenant on this node. Registers datasets and services, uploads data, chooses what is discoverable and to whom, sets the credential the node uses to reach an upstream service, and reads the access log for their own organisation. Theirs is the only role that may change what the organisation publishes.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [organisation-administrator](organisation-administrator.md) | [src/circuless_node/resources.py](../../src/circuless_node/resources.py) | Publishes and manages resources | HTTPS |
| [organisation-administrator](organisation-administrator.md) | [src/circuless_node/access_log.py](../../src/circuless_node/access_log.py) | Reads their own access log | HTTPS |
