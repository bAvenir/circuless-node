---
type: C4 Container
title: Node application
status: stable
groma:
  id: node-application
  parent: circuless-node
  technology: Python 3.12, FastAPI, Uvicorn, SQLModel
description: 'The node itself: two HTTP applications on two sockets, the access decision, and everything that stores or serves a tenant''s data.'
---

Runs as a single process serving **two applications on two sockets**, and the split is a security boundary rather than tidiness.

The **public** application on 8000 is what the gateway and the overlay reach. Every route on it requires a valid token and lives under `/v1`; it serves no schema and no docs, because an unauthenticated index of every route is worth a great deal to anyone scanning.

The **internal** application on 8001 binds loopback or the overlay address and carries `/internal/authz`, `/metrics` and `/healthz`. The gateway is never routed to it — its rule matches `/v1` and nothing else — so those endpoints are unreachable from the internet by construction rather than by a deny list somebody maintains.

Work enters as an HTTP request carrying a token, is resolved to a subject, scoped to a tenant, decided against the cached agreements, served or refused, and recorded. The database is SQLite in the usual deployment and PostgreSQL where one is available.
