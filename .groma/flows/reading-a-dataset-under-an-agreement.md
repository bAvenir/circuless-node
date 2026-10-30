---
type: Groma Flow
title: Reading a dataset under an agreement
groma:
  id: reading-a-dataset-under-an-agreement
---

The platform's central scenario, and the one every security rule here is shaped by: a consumer from another organisation reads bytes the provider never sent anywhere.

What matters is **where the decision is made**. Not in the Cloud, not at the gateway, and not once at the start of the relationship — this node decides, on this request, against agreements it pulled within the last thirty seconds. That is why revoking access works: there is no copy to recall, only a decision that stops coming out the same way.

The refusal path matters as much as the success path. Before an agreement exists the same call returns a reason code saying so, which is how a consumer knows to ask rather than to report a fault.

## Steps

| From | To | Action |
| --- | --- | --- |
| [Data consumer](../actors/data-consumer.md) | [Data transfer and storage](../systems/circuless-node/containers/node-application/components/data-transfer.md) | Requests the dataset, presenting a token issued for this node and no other |
| [HTTP application](../systems/circuless-node/containers/node-application/components/http-application.md) | [Token verification and subject resolution](../systems/circuless-node/containers/node-application/components/token-verification.md) | Before any handler runs: signature, issuer, audience and clock, then the caller's organisations resolved from the token alone — never from the request body |
| [Token verification and subject resolution](../systems/circuless-node/containers/node-application/components/token-verification.md) | [Tenancy](../systems/circuless-node/containers/node-application/components/tenancy.md) | Scopes everything that follows to the tenant named in the path |
| [Cloud synchronisation](../systems/circuless-node/containers/node-application/components/sync.md) | [Database and schema](../systems/circuless-node/containers/node-application/components/database.md) | The agreements this will be judged against were written here by the last pull, at most thirty seconds ago |
| [Data transfer and storage](../systems/circuless-node/containers/node-application/components/data-transfer.md) | [Access decision](../systems/circuless-node/containers/node-application/components/access-decision.md) | Asks the pure decision function, passing those agreements and the current time. No agreement, no bytes |
| [Data transfer and storage](../systems/circuless-node/containers/node-application/components/data-transfer.md) | [Access log](../systems/circuless-node/containers/node-application/components/access-log.md) | Records the decision, and the byte count once the stream finishes — allow or deny, either way |
