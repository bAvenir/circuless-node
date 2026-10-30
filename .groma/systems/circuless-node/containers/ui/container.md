---
type: C4 Container
title: Node admin UI
status: stable
groma:
  id: ui
  parent: circuless-node
  technology: Static HTML and JavaScript, PKCE in the browser
description: The operator's browser interface to one node, served by the node from files inside its own package.
---

A small static application — no build step, no framework — that an organisation's admins use to see their tenants, register and inspect resources, and read the access log.

It is served from `/ui`, which is **the one path on a node that does not require a token**: a browser cannot present a token for the request that fetches the code which then obtains one. What may be served there is therefore constrained and asserted in the node's own startup checks — static files and a `config.json` of exactly four keys. No tenant data, no node state.

Nothing is fetched from the internet to render it, which is a requirement for a node on a partner's premises.
