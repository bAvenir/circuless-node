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

## Every route needs a token

`/v1/whoami` is the only route so far. It reports what the node makes of your token, which
is the M1 demo, a way for someone installing a node to confirm it reads their tokens, and
the thing that gives the route-auth test something real to check. It echoes only claims the
caller already holds.

```sh
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/v1/whoami
```

Verification runs offline — the node never calls the Cloud on the request path (§3.7) — in
this order:

| | Check | Failure |
|---|---|---|
| 1 | signature, against the cached JWKS | 401 |
| 2 | `iss` matches the configured issuer | 401 |
| 3 | `aud` is **exactly** `node:{node_id}` | 401 |
| 4 | `exp`/`nbf`, 60 s leeway for clock drift | 401 |
| 5 | `principal_type` is not `node` | **403** `node_principal_not_permitted` |

Steps 1–4 ask "is this real and meant for me". Step 5 asks "may this principal speak here
at all" — the token is genuine, the caller simply is not permitted, so 403 rather than 401.

Three details that are easy to get wrong:

**`aud` must be this node's audience and no other.** PyJWT is satisfied when the expected
audience appears *among* several, which is right for OAuth generally and wrong here: a
token naming two nodes is replayable between them (§5.4).

**A missing `principal_type` is refused, not treated as `user`.** Keycloak's User Profile
cannot default an attribute, so the claim can genuinely be absent — and reading that as
`user` would let a node principal whose attribute was never set pass the check meant to
reject it.

**Only asymmetric algorithms are accepted.** Allowing HMAC is the classic confusion attack:
the public key everyone can read becomes the secret an attacker signs with.

The JWKS cache refetches **once** on an unknown `kid`, throttled to one fetch a minute —
without that, a key rotation rejects every token until someone restarts the node, and with
it unthrottled an invented `kid` becomes a way to make the node hammer Keycloak. Stale keys
beat no keys when Keycloak is unreachable (F16); the cache is in memory only, because an
outage longer than the 5-minute token lifetime stops consumption anyway (§3.7).

### Enumerating routes is harder than it looks

The no-anonymous-route gate and the `/v1` prefix test both assert things about whatever
`tests/harness/routes.py` returns, so an enumerator that under-reports makes both pass by
checking a shorter list. Three ways of writing it have now failed silently across the two
repositories, and `tests/test_route_enumeration.py` pins each one:

- `app.routes` filtered for `APIRoute` **misses included routers** — it returned nothing
  here until `/v1/whoami` was added and the count stayed at zero;
- `app.openapi()` **misses `include_in_schema=False`** — what this module used until the
  backport. The node has no such route yet, which is exactly the problem: the gate would
  have stopped covering the first one silently;
- walking route objects **loses a nested router's prefix**, reporting `/orgs` for a route
  served at `/v1/orgs` — so the gate requests a path that does not exist and reads the
  404 as "not 401".

## Who is asking, and on whose behalf

`require_subject` gives a handler one normalised `Subject`, so no handler ever reads a
claim and interprets it its own way (§3.2). Identity comes from the token and never from
the request body (R3, D17).

| | |
|---|---|
| A user's organisations | depth-one groups: `/orgs/alpha`. Anything deeper, and anything outside `/orgs`, is ignored (G14) |
| Admin of an organisation | membership of `/orgs/alpha/admins`, and of that organisation alone (R17) |
| A service's organisation | the `org_id` claim, never groups — and a service is **never** an org admin (N18) |
| The actor | `azp`. Token exchange carries no `act` claim, so this is the only trace of a service acting for someone |

**Being an admin implies being a member.** Keycloak does not require membership of the
parent group, and admin rights should not depend on how carefully someone clicked.

**An unrecognised subgroup confers nothing** — not even membership. `/orgs/alpha/teams/blue`
makes you neither a member nor an admin of alpha.

### Acting organisation

People work for more than one organisation, so which one a request is made on behalf of is
resolved per request (R11), by intersecting the subject's organisations with those that
could authorise *this* request:

- one match → that one;
- several → the caller sends `X-CIRCULess-Acting-Org`;
- none, or a header naming an organisation they are not in → refused.

The header grants nothing: it is checked against membership, so it can only narrow a
choice the subject already had. Naming someone else's organisation is refused rather than
ignored — quietly acting as a different organisation than the caller asked for is worse
than an error, because they would never find out.

Auditors ask on whose behalf a consultant read a file. This is the answer, and N11 logs it.

## The node's own identity

A node authenticates to the Cloud API as **infrastructure** — a `node_id`, no
organisation — and its tokens are refused at every consumption endpoint (D14). It can push
its catalogue, pull agreements and heartbeat, and nothing else.

It uses **`private_key_jwt`** (D18), so no secret is ever sent to anyone. The keypair is
generated on first start and only the public half, in a self-signed X.509 certificate
(D26), is registered with Keycloak. Nothing to put in a handover email, in git, or in the
realm export.

Enrollment is manual in the beta (§3.6):

```sh
circuless-node certificate    # print it; an operator registers it on the node's client
circuless-node check          # confirm the node can obtain a circuless-cloud token
```

`check` is M1's exit criterion on demand, and it is the first thing to run on an install
that is not working. Before the certificate is registered it says exactly that rather than
leaving someone reading Keycloak logs.

The private key is written `0600` at creation, not chmod-ed afterwards, so it is never
briefly world-readable. **The node refuses to start on a key anyone else can read** —
fixing it silently would hide that something loosened it, and on a shared host the key may
already have been copied.

A node token asks for `scope=circuless-cloud` rather than being given that audience by
default. One audience per token (§5.4) applies to nodes too: as a default it would ride
along on every node token, and one that also named a node audience would carry two.

Losing the private key means generating a new one and re-registering it; losing the Fernet
key (N10) means re-entering upstream credentials. Both are documented rather than
engineered around (G11), and both belong in a key backup kept separate from the data
backup.

## One process, several organisations

Every tenant-owned query is filtered once, centrally, by a session-level
`with_loader_criteria` (N4). No handler writes `WHERE tenant_id = ...` itself, so a handler
that forgets cannot leak anything. Isolation here is **code-enforced, not OS-enforced** —
a declared limitation (§4.3) that belongs in D2.1 and the T2.7 material.

```python
with tenant_scope(session, tenant.id):
    ...  # only this tenant's rows exist

with all_tenants(session):
    ...  # deliberately across tenants — the purge job (N20), and little else
```

**A table opts in by subclassing `TenantOwned`**, so the model's shape decides, not a list
someone has to remember to update. The node-global tables — `AgreementCache`, `OrgMap`,
`NodeIdentity` — deliberately do not subclass it: filtering them would break sync, because
the agreements a node enforces belong to no single tenant (R10).

**An unscoped query raises rather than returning everything.** If the filter quietly did
nothing when no tenant was bound, forgetting to bind would disable isolation while looking
exactly like working code.

**Writes are checked too.** `with_loader_criteria` only touches SELECT, so a separate
`before_flush` check refuses a row whose `tenant_id` is not the bound one — otherwise a
handler could insert under someone else's tenant and then never see the row again.

### Scoping is not authorisation

This layer guarantees a query about tenant A returns only tenant A's rows. It says nothing
about whether *this caller* may act on tenant A — that is `decide()` for consumption (N6)
and management authorisation for the rest (N18). Conflating the two is how a hole appears:
a caller from another organisation reaching `/v1/t/alpha/...` gets correctly-scoped Alpha
data unless something else stops them.

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

## Registering datasets and services (N5)

```
POST   /v1/t/{tenant}/resources                register a dataset or a service
GET    /v1/t/{tenant}/resources                list this tenant's resources
GET    /v1/t/{tenant}/resources/{id}
PATCH  /v1/t/{tenant}/resources/{id}
```

The provider sends typed fields and the node composes the DCAT-AP record from them
(`dcat.py`). That choice is what makes the rules enforceable: "has a licence from the
controlled list" is a column check here, and a walk over arbitrary provider JSON-LD
otherwise.

**Three rules the endpoints exist for:**

| | |
|---|---|
| Defaults are closed (NFR4) | a new resource is `discoverability=hidden`, `visibility=org` |
| A licence is required to publish (NFR9) | from `vocabularies.LICENCES`, checked against the *resulting* state so a two-step publish cannot slip past |
| A BVR-operated node refuses `sensitive` (D22) | and `operator` defaults to `bvr`, so the permissive value is the one somebody has to type |

**`discoverability` and `visibility` are independent, and easy to confuse.** The first
governs who may learn the resource *exists* — what reaches the Cloud catalogue. The second
governs who may *read or invoke* it, and `decide()` (N6) owns it. A resource listed
publicly and readable only under an agreement is the normal case.

**A service is a `dcat:DataService`**, not a Dataset with a URL in it (R15) — with
`endpointURL`, `endpointDescription` and `landingPage`. The `endpointURL` published is the
node's own `/invoke` path: a consumer who learned the real upstream address could go round
the node, past `decide()`, past the agreement check and past the log.

**Changes mark, they do not push.** Registering sets a row in `catalogue_push` and
returns; N7's loop sends the tenant's catalogue and clears it. Pushing inside the request
would make registration fail whenever the Cloud is unreachable, turning a control-plane
outage into a data-plane one — the thing F16 and D1 exist to prevent.

## Who may manage a tenant's resources (N18)

`management.py`, pure and table-tested, like the Cloud's `authz.decide()`:

| Operation | Who |
|---|---|
| Register, update, upload, delete | admins **or service principals** of the tenant's org |
| Credentials | admins only |
| Read the access log | that organisation's admins |
| Node configuration | the node client role `admin` |

A service principal may publish but may never touch credentials. That distinction is why
N18 exists: a pipeline account that can publish yesterday's run is useful, and the same
account being able to rotate the upstream credential means a compromised pipeline can
redirect where the node fetches from.

**Scoping is not authorisation.** N4 guarantees a query about tenant A returns only tenant
A's rows; it says nothing about whether this caller may act on tenant A at all. Every
handler resolves the tenant and *then* calls `enforce_management`, in that order.

## What a node says about itself (N12)

```sh
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/.well-known/circuless-node
```

Identity, how to reach it, and what it can be asked to do. **It requires a token** —
unusual for a `.well-known` path and deliberate: an unauthenticated description of a node
is a free inventory for anyone scanning, giving its version, its overlay address and
confirmation that a CIRCULess node is running here. Everyone who needs it holds a token.

It is the only public path outside `/v1` (invariant 1), listed in `UNVERSIONED_PATHS`,
which is itself asserted — so adding a second one fails a test first. `.well-known` is an
interoperability convention; versioning it would mean nobody could find it by convention.

**Two endpoints, in preference order.** A client that can join the overlay reaches the
node directly and the Cloud is not on the path at all (§5.6); the gateway is the fallback
for browsers and anything that cannot install an agent. Both come from node settings
rather than from the Cloud, because this is the document somebody reads while trying to
reach a node that is having trouble.

**It does not list the organisations hosted here.** That is the one thing in scope that
is about other people rather than about the node, and the Cloud restricts the same
mapping to `platform-admin`. It is unreachable rather than merely omitted: `node_document`
takes settings and nothing else, so it has no way to read the tenant table, and a test
asserts that signature.

### One version, derived

`__version__` comes from the installed distribution. It used to be a constant in
`__init__.py` and had already drifted — `0.1.0` against `pyproject.toml`'s `0.2.0` —
which nothing noticed because nothing read it. It is now published here, sent in the
heartbeat, and used as the OpenAPI version, so a stale constant would be the node telling
the platform three wrong things about itself. `bvr-ci` bumps the manifest; everything
else follows.

## Staying in step with the Cloud (N7)

Three exchanges, all started by the node. **The Cloud never reaches into a node** — a
node behind a partner's firewall has no inbound ports at all (D10).

| | |
|---|---|
| push | the tenant's whole DCAT catalogue, when N5 marked it dirty |
| pull | provider-side agreements and the org map, every 30 s |
| heartbeat | "still here", with the running version |

The loop is a **peer of the two servers**, not part of either: `_serve()` runs all three
with `asyncio.gather`, so there is nothing extra to install or schedule on a partner
site, and syncing stops when the node stops.

**A pull replaces; it never merges.** A revocation is an *absence* from the feed, and a
merge would never notice one — so the node would go on honouring an agreement the
provider had withdrawn. The whole provider-side set arrives each time and replaces what
is there, in one transaction.

**Only provider-side agreements arrive** (R4). A node never learns what its tenants are
buying elsewhere, which matters when the node is operated by a competitor of the other
party.

### When the Cloud is down (F16)

A failed pull changes nothing: the cache stays, `decide()` keeps answering from it, and
the node goes on serving. A control-plane outage must not become a data-plane one. The
window is bounded anyway — nobody can get a fresh token once Keycloak is unreachable, and
tokens live five minutes — so the exposure is a revocation arriving late.

**`/healthz` stays 200 when the cache is stale.** A node enforcing from a stale cache is
doing exactly what it was designed to do, and a 503 would have an orchestrator remove it
during the very outage the cache exists to survive — every node at once, since they would
all be stale together. Staleness belongs on `/metrics`, where it is an alert rather than
an eviction:

```
circuless_node_sync_age_seconds      seconds since this process last synced, or -1
circuless_node_sync_failures_total   consecutive failures
circuless_node_agreements_cached     agreements currently enforced
```

`sync_age_seconds` is the age of **this process's** last success, not of the cache — the
state is held in memory, so a restart resets it while the cache in the database survives.
Worth knowing before reading it during an incident.

A node whose certificate nobody has registered yet keeps serving and records the failure;
`circuless-node check` says so plainly.

### What is not asserted here

`tick()` talks to a `CloudTransport` and the tests drive it with a fake, so **what the
node does** is covered and **what it puts on the wire** is not. The Cloud API is in a
private repository and this one is public, so the node's CI cannot run it — the same
constraint that makes `tests/realm/` a fixture. Until the cross-repo tests (Q1) run, the
paths and payloads in `sync.HttpCloud` are a proposal that the Cloud's C5 and C6 have to
match, and nothing in this repository would notice if they did not.

## Layout

```
src/circuless_node/
    app.py        the two applications, CORS, the /v1 router
    settings.py   configuration; refuses a wildcard CORS origin
    models.py     tenant-owned vs node-global tables (R10)
    tenancy.py    the central tenant filter, and the scopes that drive it
    resources.py  N5 registration, and the rules on licence and classification
    sync.py       N7 push, pull, heartbeat, and the staleness metrics
    well_known.py N12 what the node publishes about itself
    management.py N18 who may manage a tenant's resources
    dcat.py       rendering DCAT-AP from typed fields
    vocabularies.py  the controlled lists: licences, themes, classification
    identity.py   the node's keypair, certificate and Cloud credentials
    oidc.py       discovery, shared by the JWKS cache and the credentials
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

**M1, complete:** **N1** skeleton, **N14** storage, **H3**'s `/v1` prefix, **Q1** test
harness, **N2** token verification, **N3** subject resolver, **N4** tenancy, **N17** node
self-authentication.

**M2, in progress:** **N5** resource registry, **N18** management authorization, **N7**
sync client, **N12** `/.well-known/circuless-node`.

Next: **N15** the NetBird client, then the remote dry run (Q3) on a machine outside BVR's
network.

Not yet built, and deliberately absent rather than stubbed: `decide()` (N6), uploads,
and deletion. Deletion is two-stage (D25, N20 in M3), so there is no `DELETE` at all — a
placeholder that actually removed a row would be the wrong thing to have to take back.
`ResourceStatus.WITHDRAWN` exists from the start, and every query already excludes it, so
N20 does not have to find the one that forgot.
