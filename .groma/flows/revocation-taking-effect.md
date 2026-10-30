---
type: Groma Flow
title: Revocation taking effect
groma:
  id: revocation-taking-effect
---

The platform promise about withdrawing access, and the one scenario that is about time rather than about a request.

Nothing is recalled, because nothing was handed out. A consumer never held a permission — they held a token, and every request was decided afresh against agreements this node had pulled within the last thirty seconds. Revoking changes what the next pull returns, and the same call then comes out the other way.

That gives the headline number: at most thirty seconds for the pull, plus whatever is left of the five-minute token the consumer is already holding. The token is why the second term exists — it is valid until it expires and the node cannot un-issue it, but it only ever got the caller as far as the decision.

The deny is logged exactly as the allow was. An audit log that recorded only successes would answer the least interesting question.

## Steps

| From | To | Action |
| --- | --- | --- |
| [Cloud synchronisation](../systems/circuless-node/containers/node-application/components/sync.md) | [CIRCULess Cloud](../externals/circuless-cloud.md) | Pulls the provider-side agreements every thirty seconds; the revoked one is simply no longer in the feed |
| [Cloud synchronisation](../systems/circuless-node/containers/node-application/components/sync.md) | [Database and schema](../systems/circuless-node/containers/node-application/components/database.md) | Replaces the cached agreements. If the Cloud were unreachable the node would keep deciding from the previous cache and report itself stale, rather than failing open or closed at random |
| [Data consumer](../actors/data-consumer.md) | [Data transfer and storage](../systems/circuless-node/containers/node-application/components/data-transfer.md) | Repeats the request that worked a minute ago, with a token that has not expired yet |
| [Database and schema](../systems/circuless-node/containers/node-application/components/database.md) | [Tenancy](../systems/circuless-node/containers/node-application/components/tenancy.md) | Loads the agreements now cached, filtered to this tenant |
| [Data transfer and storage](../systems/circuless-node/containers/node-application/components/data-transfer.md) | [Access decision](../systems/circuless-node/containers/node-application/components/access-decision.md) | Asks the same pure function with the same inputs but for the agreement that is gone. No agreement, no bytes |
| [Data transfer and storage](../systems/circuless-node/containers/node-application/components/data-transfer.md) | [Access log](../systems/circuless-node/containers/node-application/components/access-log.md) | Records the refusal with its reason code, so the consumer learns to ask again rather than report a fault |
