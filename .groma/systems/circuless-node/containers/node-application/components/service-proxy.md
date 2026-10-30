---
type: C4 Component
title: Service proxy and upstream credentials
status: stable
groma:
  id: service-proxy
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/proxy.py
    - scanner: python
      file: src/circuless_node/credentials.py
  group: Resources and transfer
  technology: httpx
description: Calls a tenant's own application on a consumer's behalf, without letting either see the other's secrets.
---

Once the request is allowed, the node **strips** what the caller sent — the authorization header, cookies, the host, hop-by-hop headers, and any inbound `X-CIRCULess-*`, so a caller cannot forge an identity. It **injects** the organisation's upstream credential and the headers naming the subject, organisation, resource, request and acting client. It **forwards** only an allowlist of the caller's own headers.

Redirects back into the registered endpoint are rewritten to the node's own address; a redirect anywhere else is refused, because following it would take the caller out from behind the decision and the log. Streams are passed through unbuffered, and a `202` is passed through as-is — the node holds no job state.

The credential itself is encrypted at rest, may be set or rotated by an organisation's admins only, and is returned by no API and no screen.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [src/circuless_node/proxy.py](../../../../../../src/circuless_node/proxy.py) | [upstream-service](../../../../../externals/upstream-service.md) | Forwards the decided call | HTTP |
| [src/circuless_node/proxy.py](../../../../../../src/circuless_node/proxy.py) | [src/circuless_node/decide.py](../../../../../../src/circuless_node/decide.py) | Authorizes the invocation | Python call |
| [src/circuless_node/proxy.py](../../../../../../src/circuless_node/proxy.py) | [src/circuless_node/access_log.py](../../../../../../src/circuless_node/access_log.py) | Records the decision | SQLModel |
| [src/circuless_node/proxy.py](../../../../../../src/circuless_node/proxy.py) | [examples/reference-service/service.py](../../../../../../examples/reference-service/service.py) | Forwards the decided call | HTTP |
