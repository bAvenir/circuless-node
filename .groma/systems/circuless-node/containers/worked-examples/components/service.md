---
type: C4 Component
title: Reference service
status: stable
groma:
  id: service
  parent: worked-examples
  code:
    - scanner: python
      file: examples/reference-service/service.py
description: A stand-in for a partner's application behind a node, used to demonstrate and test the proxy.
---

Shows what a service actually receives: the node's credential, the identity headers the node added, and none of what the caller sent. The acceptance cases for redirects, streaming and timeouts run against it.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [examples/reference-service/service.py](../../../../../../examples/reference-service/service.py) | [src/circuless_node/proxy.py](../../../../../../src/circuless_node/proxy.py) | Answers, possibly redirecting | HTTP |
