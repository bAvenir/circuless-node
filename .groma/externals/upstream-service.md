---
type: C4 System
title: Upstream service
status: stable
groma:
  id: upstream-service
  technology: HTTP
---

An application the hosting organisation already runs, fronted by this node rather than exposed. The node decides the request, strips what the caller sent, attaches the organisation's own credential and forwards the call; the caller never sees the credential.
