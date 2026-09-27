# CIRCULess Node

The data plane of the CIRCULess core platform. It holds organisations' datasets, fronts
their services, and **decides every access locally** — no call to the Cloud on the request
path. One process hosts several tenants. It runs in the Cloud or on a partner's premises,
reaching the rest of the platform over a WireGuard overlay with no inbound ports.

Design, decisions and component IDs: `docs/architecture/`. Security invariants that must
never be broken, and what each one is for: `CLAUDE.md`.

## Running it

```sh
uv sync
cp .env.example .env          # CIRCULESS_NODE_NODE_ID is the only required value
uv run alembic upgrade head
uv run circuless-node
```

```sh
uv run pytest        # tests — starts a Keycloak if one is not already running
uv run ruff check .  # lint
```

## Tests run against a real Keycloak

Never a mocked issuer. The node's whole job is deciding what a token means, and a fake
issuer would agree with whatever the node believed.

The suite starts its own Keycloak on `127.0.0.1:8090` and builds a fixture realm through
the Admin API. **If one is already running it is reused**, which is the difference between
a 26-second run and a 4-second one — worth knowing, because this suite grows for the rest
of the project:

```sh
docker compose -f tests/compose/docker-compose.yml up -d     # leave it up
docker compose -f tests/compose/docker-compose.yml down -v   # start clean
```

Point the tests elsewhere with `CIRCULESS_TEST_KEYCLOAK_URL`.

**User tokens come from the authorization-code flow**, driven end to end with PKCE — not
the password grant, which H2 disables on every client (SR-1.1.4). Service and node
principals use `private_key_jwt`, which H2 does not affect.

`tests/realm/` is a **fixture, not a copy of the production realm**, which lives in the
private `circuless-cloud` repository. This repository is public, so coupling its CI to a
private one would mean a secret in public settings and broken CI on every fork PR. The two
sides meet at a written contract instead: `tests/realm/CONTRACT.md` states exactly which
claims the node depends on, the cloud's `kc.py verify` asserts the realm emits them, and
this suite asserts the node consumes them. **Change one side, change the other.**

## Two sockets, not one

The node serves two applications on separate ports, and which port something is on *is* the
access control:

| | Port | Serves | Reachable from |
|---|---|---|---|
| **public** | 8000 | everything under `/v1` | the gateway and the overlay |
| **internal** | 8001 | `/healthz`, `/metrics`, `/internal/authz`, API docs | loopback or the overlay only |

They are separate ASGI apps rather than one app with a guard. `/internal/authz` is an
authorization oracle — anyone who can call it can ask "may X read Y?" repeatedly — and
`/metrics` and the schema are reconnaissance. R8 requires them to be unreachable through the
gateway, and **an endpoint that is not on the socket cannot be reached by forging a header**.

`/healthz` returns a status code and no body: a body would leak version and sync state to
anything that can open the socket.

## `/v1` on everything

Every public route carries the prefix (D24), enforced by construction — the public app
mounts a single router that holds it — and asserted by a test. This one is genuinely "now or
never": after M1, changing it breaks every client and every test at once.

The only paths that ever live outside `/v1` are the internal three above, plus
`/.well-known/circuless-node`, which arrives with N12 and requires a platform token.

## Layout

```
src/circuless_node/
    app.py        the two applications, CORS, the /v1 router
    settings.py   configuration; refuses a wildcard CORS origin
    models.py     tenant-owned vs node-global tables (R10)
    db.py         engine; SQLite in WAL mode
    storage.py    fsspec adapter, and path confinement
    errors.py     reason codes — one enum, no free text
migrations/       Alembic, from the first model
tests/
```

**Tenant-owned versus node-global** is load-bearing. Tables carrying `tenant_id` are filtered
once, centrally (N4). `AgreementCache`, `OrgMap` and `NodeIdentity` are node-global and
excluded — applying the filter to them would break sync, because the agreements a node
enforces belong to no single tenant.

**Path confinement lives in the storage adapter**, not only in the handlers. `/data/{path}`
and `/invoke/{path}` get their own checks in H3, but every read and write in the node passes
through `Storage.resolve`, so this is the one place that cannot be bypassed by a handler that
forgot. It refuses `..`, absolute paths, and scheme-relative or absolute URLs (SR-3.2.3).

## Configuration

All settings take the `CIRCULESS_NODE_` prefix; see `.env.example`. Two worth knowing:

`CIRCULESS_NODE_NODE_ID` is required and must match the Keycloak client scope exactly —
tokens are accepted only for audience `node:<node_id>`.

`CIRCULESS_NODE_CORS_ALLOW_ORIGINS` is an exact list and the node **refuses to start** on
`"*"` (G7). Browser downloads through the gateway are cross-origin and carry a bearer token,
so a wildcard would let any site spend a user's node token.

## State of the build

Built: **N1** skeleton, **N14** storage, **H3**'s `/v1` prefix, **Q1** test harness.
Next: token verification (N2), the subject resolver (N3), tenancy (N4) and node
self-authentication (N17).

There are no `/v1` routes yet, so the route-auth test skips rather than passing vacuously.
That is expected — the prefix and the interface split had to be settled before any route
existed, which is the whole reason they come first.
