---
type: Groma Flow
title: Invoking a service through the node
groma:
  id: invoking-a-service-through-the-node
---

The other half of what a node does. A dataset is bytes the node holds; a service is an application the hosting organisation already runs, which the node fronts rather than exposes.

The decision is the same one — the same pure function, the same cached agreements, the same log entry. What differs is everything after it. The node strips what the caller sent, attaches the organisation own credential, and forwards. The caller never learns the credential, and never learns the upstream address: the catalogue record points at the node, because a consumer holding the real endpoint could go round the decision, the agreement and the log.

Two details exist for the same reason. A redirect back into the registered endpoint is rewritten to the node own address and a redirect anywhere else is refused, because following one would take the caller out from behind the node. And a 202 is passed through untouched — the node holds no job state, so a long-running service stays the service problem.

## Steps

| From | To | Action |
| --- | --- | --- |
| [Data consumer](../actors/data-consumer.md) | [Service proxy and upstream credentials](../systems/circuless-node/containers/node-application/components/service-proxy.md) | Calls the service through the node, presenting a token issued for this node and no other |
| [HTTP application](../systems/circuless-node/containers/node-application/components/http-application.md) | [Token verification and subject resolution](../systems/circuless-node/containers/node-application/components/token-verification.md) | Signature, issuer, audience and clock, then the caller organisations — resolved from the token, never from the request |
| [Token verification and subject resolution](../systems/circuless-node/containers/node-application/components/token-verification.md) | [Tenancy](../systems/circuless-node/containers/node-application/components/tenancy.md) | Scopes the lookup to the tenant named in the path |
| [Service proxy and upstream credentials](../systems/circuless-node/containers/node-application/components/service-proxy.md) | [Access decision](../systems/circuless-node/containers/node-application/components/access-decision.md) | The same pure function that decides a dataset read, asked for the invoke action instead |
| [Service proxy and upstream credentials](../systems/circuless-node/containers/node-application/components/service-proxy.md) | [Access log](../systems/circuless-node/containers/node-application/components/access-log.md) | Records the decision before anything is forwarded, so a dropped connection still leaves the record |
| [Service proxy and upstream credentials](../systems/circuless-node/containers/node-application/components/service-proxy.md) | [Upstream service](../externals/upstream-service.md) | Strips the caller authorization, cookies, host and any inbound X-CIRCULess headers; injects the organisation credential and the headers naming the subject; streams the answer back unbuffered |
