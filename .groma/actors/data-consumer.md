---
type: C4 Actor
title: Data consumer
status: stable
groma:
  id: data-consumer
---

A person or application from another organisation that has found a record in the CIRCULess catalogue and wants the data behind it. Reaches this node directly — through the public gateway from a browser, or over the overlay — and is allowed or refused by this node on every request, according to the agreements it holds.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [data-consumer](data-consumer.md) | [src/circuless_node/transfer.py](../../src/circuless_node/transfer.py) | Downloads a dataset | HTTPS |
| [data-consumer](data-consumer.md) | [src/circuless_node/proxy.py](../../src/circuless_node/proxy.py) | Invokes a service | HTTPS |
