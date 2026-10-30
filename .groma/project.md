---
type: Groma Project
title: CIRCULess Node
groma:
  profile: architecture
description: 'The data plane of the CIRCULess platform: it holds an organisation''s data and decides every access to it locally.'
---

A CIRCULess Node holds the datasets and fronts the services of one or more organisations, and makes the access decision for every request itself — never the Cloud, never the network, never the gateway.

That is the point of the whole platform: a provider publishes a *description* to the CIRCULess Cloud and keeps the data. A consumer finds the description, asks for access, and the provider's admin agrees. Only then does this node serve bytes, and it re-checks the agreement on every single request, so revoking one stops access within about half a minute rather than asking somebody to delete a copy.

One process hosts several organisations as separate tenants. It runs in Bavenir's cloud or on a partner's own premises, reaching the rest of the platform over an encrypted overlay it joins by connecting outwards — so a node on a partner's site needs no inbound firewall ports.

Target is TRL 5: a beta demonstrated in a relevant environment. Correctness and the security invariants matter more than features.
