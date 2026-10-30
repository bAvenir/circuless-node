---
type: C4 Container
title: Worked examples
status: stable
groma:
  id: worked-examples
  parent: circuless-node
  technology: Python
description: A reference service and a client, used to demonstrate the integration contract and to exercise it in the acceptance tests.
---

Not part of the node. These are the examples a partner is pointed at when integrating.

The **reference service** stands in for a partner's own application behind a node: it shows what a service actually receives once the node has decided the request, stripped what the caller sent and attached the organisation's credential. The acceptance cases for the service proxy run against it.

The **client example** drives the three ways an application can call a node — as a browser, as itself, and acting for a signed-in person through token exchange — printing the claims of each token so that the difference is visible rather than described.
