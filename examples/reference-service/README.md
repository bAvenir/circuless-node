# The CIRCULess reference service

A CSV service — give it a CSV, it tells you the column names. The processing is beside
the point. What this shows is **how little a service has to change** to be reachable
through a CIRCULess node, and what it gains if it chooses to change more.

> Not the node's admin UI. That is N13, inside the node itself. This is an example of a
> **partner's** service and a **partner's** app.

`INTEGRATING-A-SERVICE.md` in the repository root is the contract. This is a worked
example of it.

## The tiers

| | what it is | what it costs |
|---|---|---|
| **0** | `service.py` — an ordinary API with an `X-API-Key`. Reads no CIRCULess header, does not know the platform exists | **nothing.** If your service already has an API key, it is already tier 0 |
| **1** | `identity.py` — adds per-user behaviour from `X-CIRCULess-Subject` | about forty lines, in one file you can read in a minute |
| **2** | fully integrated: your logs carry the node's request id, `Retry-After` paces polling, redirects resolve inside your registered endpoint | a handful of lines, each buying something specific |

`service.py` and `identity.py` are deliberately separate files, and `identity.py`
imports the other rather than replacing it. The diff between running `service:app` and
`identity:app` **is** the cost of integrating.

| file | |
|---|---|
| `service.py` | tier 0, FastAPI |
| `identity.py` | tier 1, imports the above and adds to it |
| `minimal.py` | tier 0 again, in `http.server`, no framework — proof the contract is HTTP |
| `docker-compose.yml` | the arrangement. Note what it does *not* publish |
| `Dockerfile` | non-root, read-only, pinned |

## What the node does, so your service need not

Keycloak issued the caller's token. The node verified it, resolved the person and the
organisation they are acting as, checked the agreement that permits this call, and
wrote an access-log entry. Only then did it call you, with your own API key.

So **do not authenticate the end user**. The user's token never arrives — the node
strips `Authorization` so your service can never replay it against the node. What
arrives at tier 1 is a pseudonymous `sub` and an organisation. There is no name and no
email, by design.

**The one service that cannot be tier 0** is one that already requires an end-user OIDC
token of its own. It will need the tier-1 change, because the token it is expecting is
exactly the one the node removes.

## Running it

```sh
export REFERENCE_SERVICE_KEY=$(openssl rand -hex 24)
docker compose up -d --build            # tier 0
SERVICE_MODULE=identity docker compose up -d   # tier 1
```

Then register it on the node and hand over the same key:

```sh
POST /v1/t/beta/resources
{ "slug": "csv-tools", "kind": "service", "title": "CSV tools",
  "theme": "processing", "classification": "non-sensitive",
  "endpoint_url": "http://reference-service:8080" }

PUT /v1/t/beta/resources/<id>/credential
{ "scheme": "header", "header_name": "X-API-Key", "secret": "<the same value>" }
```

Org admins only on that second call, and no API ever returns the value again.

## What each endpoint demonstrates

| endpoint | |
|---|---|
| `POST /headers` | the core case: **the caller sends the bytes** |
| `POST /headers` with an empty body → `422` | your error body reaches the caller unchanged |
| `POST /jobs` → `202` + `Location` + `Retry-After` | the node passes `202` through and holds no job state |
| `GET /jobs/{id}` | the rewritten `Location` is a URL that works, and **every poll is decided again** |
| `GET /redirect/inside` → `303` | rewritten to the node's `/invoke` |
| `GET /redirect/outside` → `302` | **refused** with `502` |
| `GET /whoami` *(tier 1)* | exactly which headers arrive |

## Three things that catch people

**Send `Location` relative, without a leading slash** — `jobs/7`, not `/jobs/7`. The node
resolves it against your registered `endpoint_url` and refuses anything landing outside.
A service registered at `https://host/api` answering `/jobs/7` points at
`https://host/jobs/7`, outside what was registered, and gets a `502`.

**Set `Retry-After`.** Every poll is a separate decision and a separate access-log entry.
A client polling every 200 ms writes three hundred entries a minute.

**Do not fetch from the node.** This service holds no node credentials and makes no
outbound calls. One that fetched would be reading on *its own* authority rather than the
caller's — a user with no agreement could ask it for something it happens to be allowed
to see, and the agreement would stop being the thing that decides. The caller downloads
and posts the bytes, which bounds a request at `invoke_policy.max_request_bytes`, 10 MiB
by default. A dataset reference that the node resolves itself is the planned answer for
larger data; it does not exist yet.

## What is not demonstrated

**Server-sent events.** The node streams SSE unbuffered and forwards the MCP session
headers for a resource registered `streaming: true`; this service uses neither. Recorded
rather than omitted — see `SECURITY-DEFERRALS.md`.
