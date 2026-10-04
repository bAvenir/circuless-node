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
| `discoverability=public` is refused | reserved for later (§5.2): it means discoverable *anonymously*, and D21 allows no anonymous access, so there is nothing for it to mean yet |
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

## Deciding access (N6)

```python
decide(subject, action, resource, owner_org, agreements, now, requested_acting_org) -> Decision
```

**Pure.** No database, no clock, no request — agreements and `now` are inputs, loaded by
the caller. That is what lets the whole decision table be tested without a server, and
this function carries most of the product's security properties, so exhaustive testing
had to be cheap.

| `visibility` | allowed |
|---|---|
| anything **withdrawn** | nobody (D25) — `not_found`, so a gone resource is indistinguishable from one that never existed |
| `private` | admins of the owning organisation only, **not** ordinary members |
| `org` | any user or service of the owning organisation |
| `agreement` | the owner, plus any organisation holding a matching agreement |
| `public` | any authenticated user or service — never anonymous, never a node |

A node principal is refused before any of that (D14). N2 already rejects node tokens;
this is the second lock, because that is the rule an attacker reaches by stealing a
node's key rather than a person's password.

An agreement matches only if **all** of: its provider owns this resource, its status is
exactly `accepted`, it names this resource or none at all, it permits this action, and
`now` is inside `[valid_from, valid_until)` — half-open, so `valid_until` means "until"
rather than "through".

`decide()` returns the **acting organisation** on an allow, which N11 logs: it is the
answer to "on whose behalf did this person read that file". It is `None` for `public`,
where nothing of the caller's is what permitted it.

### R11 lives in one place

`subject.acting_org()` is pure and returns a result; `subject.resolve_acting_org()` is
the raising wrapper the handlers use. Two shapes, one implementation — written
separately, the rule that a header naming someone else's organisation is *refused rather
than ignored* would end up holding in only one of them.

### The decision table, and why it is built the way it is

`tests/test_decide.py` has two kinds of test:

- **written rows** — each a deliberate claim about what should happen and why;
- **exhaustive sweeps** over visibility × principal × agreement status × time, asserting
  only the **absolutes**: a node principal is denied in every combination, a withdrawn
  resource is denied in every combination, only `accepted` ever permits, another
  provider's agreement never grants anything.

Generating the full product with computed expectations was the alternative, and is a
trap: the expectations would come from a second implementation of `decide()`, and when
the two disagreed nothing would say which was right. A sweep's assertion does not depend
on the matrix, which is what keeps it from being vacuous.

Verified by mutation rather than assumed: removing the node check fails 144 cases,
removing the withdrawn check fails 432, making the time window inclusive at both ends
fails exactly the boundary row, and accepting any agreement status fails 25.

## Serving the bytes (N8)

```
GET /v1/t/{tenant}/resources/{id}/data          a file, or a bucket's manifest
GET /v1/t/{tenant}/resources/{id}/data/{path}   one object of a bucket
```

**One order, and it is the point of the module.** Resolve the tenant, load the resource,
`decide()`, **log**, then touch the filesystem. Every refusal below the decision is a
refusal to somebody already allowed; nothing above it can be learned without permission
— not whether a file exists, not its size, not what a bucket contains. A test pins this
by asking for a resource with no data and one with data as an outsider, and requiring
the same 403 for both.

Path validity is checked *after* the decision too. Whether `../../etc` is a legal path is
not an authorisation question, and answering it first would hand the grammar to someone
with no rights.

**Withdrawn versus never existed.** A withdrawn resource is a decision — `decide()`
denies it `not_found` (D25) and the denial is logged. A resource that never existed is
not a decision: there is nothing to record it against, and logging it would let anyone
fill a tenant's log by guessing UUIDs. It is a bare 404.

**No Range in the beta.** `Accept-Ranges: none`, always 200, always the whole file. Said
out loud rather than by omission, so a client does not attempt to resume and silently
re-download believing it appended. If it is ever added, the one thing to remember is that
Range must be parsed **after** the decision and the log write — a `416` carries
`Content-Range: bytes */size` and would otherwise disclose a file's size to someone with
no agreement.

**Buckets are decided and logged per object.** Nothing in the model expresses per-object
policy, so every object of a bucket necessarily gets the same answer. Deciding again
anyway buys two things: the log shows *which* of a 500-file campaign someone pulled, and
since agreements refresh every 30 s, a revocation takes effect mid-campaign rather than
the manifest acting as a bearer token for the whole bucket. The manifest is recursive,
capped at 10 000 objects with a `truncated` flag, and an empty or absent directory is
`objects: []` rather than an error — registered-before-uploaded is a legitimate state.

**Media type is guessed** from the filename, falling back to `application/octet-stream`.
`Resource` carries no media type in the beta; a real `dcat:mediaType` field is M4 work.

**`bytes` is filled in after the stream, not in a `finally`.** A client that disconnects
leaves it null, which N11 defines as "granted, did not complete". A partial count in the
same column as a completed one would be worse than nothing.

### Path confinement is a refusal, not a crash (H3)

`Storage.resolve` has always refused `..`, absolute paths and URLs, but until N8 nothing
could reach it — so it had no handler, and a traversal would have returned **500
`internal_error`** while `PATH_NOT_ALLOWED` sat unused in the enum. There is now an
exception handler, so `/data/{path}`, `/invoke/{path}` (N9) and uploads (N19) are covered
by construction rather than one at a time.

The grammar check is also split out as `check_relative_path`, because a caller that
*joins* a supplied segment onto a stored prefix must check it **before** the join:
`PurePosixPath` collapses repeated slashes, so joining first turns `https://evil/x` into
the perfectly ordinary `https:/evil/x` and the URL rule never fires on what was sent. A
failing test found that, not a review.

## Receiving the bytes (N19)

```
PUT /v1/t/{tenant}/resources/{id}/data          a file's bytes
PUT /v1/t/{tenant}/resources/{id}/data/{path}   one object of a bucket
```

**The mirror of N8 in the URL and nothing like it in authorisation.** Reading is
`decide()` — visibility, agreements, acting organisation. Writing is `decide_management`
(N18): admins **or service principals** of the organisation that owns the tenant. An
agreement never grants write access, and there is no visibility under which an outsider
may upload. The two live in separate modules precisely because they share a URL.

### The node owns the layout, and that is a security property

Bytes live at `<tenant_id>/<resource_id>/<name>`. `storage_path` names the file *within*
that directory and defaults to the slug; it no longer decides the location.

This changed in N19, because the old arrangement had a hole. `storage_path` was
provider-supplied and confined only to the tenant directory, so **nothing stopped two
resources of the same tenant naming the same file.** Since N18 lets a service principal
register resources, a compromised pipeline account could register a resource with
`visibility=agreement` pointing at an existing **private** resource's path and publish
its contents — without uploading a byte. Deriving the directory from the resource id
makes that unsayable: there is no `storage_path` that names another resource's data.

A `storage_path` containing `..` is now refused at registration rather than at first
upload, so the provider is told when they typed it.

### Nothing is overwritten until it has fully arrived

Every upload streams to a temporary name and is renamed into place only at the end. A
transfer that fails, or runs past the size limit, leaves the previous bytes exactly as
they were — which matters because N8 streams straight off disk, so without this a
refused upload would have truncated a file that readers were mid-way through serving.

The staging file lives in `<tenant_id>/.incoming/`, **outside** every resource
directory. Inside, it would be listed by a bucket's manifest as an ordinary object and
could be fetched through `/data/{path}` while still being written — serving exactly the
half-written file staging exists to prevent. Keeping it in a sibling directory makes
that impossible by construction rather than by remembering to filter it out.

### The size limit is checked twice

`CIRCULESS_NODE_MAX_UPLOAD_BYTES`, 1 GiB by default. Checked on `Content-Length` before
anything is read, and **again while streaming** — because a chunked request carries no
`Content-Length` at all, and a limit that only reads the header is one that any client
can skip by not sending one. Over the limit is `413 payload_too_large`.

Writes go through `anyio.to_thread`: the handler is async, and a synchronous write of a
gigabyte would hold the event loop for the whole upload, stalling every other request on
the node — including the downloads competing for the same disk.

An upload records its byte count in the same AccessLog column a download does, and marks
the tenant's catalogue dirty, since size and modification date have changed. Marked, not
pushed — pushing here would make uploads fail whenever the Cloud is unreachable (F16).

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
handler resolves the tenant and *then* decides, in that order — both in
`_authorised_tenant`, which is also where the decision gets logged (N11), so none of the
three can be forgotten at one call site.

## Every decision is recorded (N11)

`GET /v1/t/{tenant}/access-log`, for that organisation's admins. One row per decision —
allow and deny, consumption and management — carrying the request id, resource, action,
pseudonymous `sub`, principal type, actor (`azp`), acting org, decision, reason and bytes.

Three things about it are worth knowing before you read the code.

**It is written outside the request's transaction.** `access_log.record()` takes the
*engine*, not a `Session`, and that signature is the guarantee rather than an
inconvenience. A denial is followed by a raised `NodeError`, the handler's session exits
without committing, and an entry written through it would be rolled back — so sharing the
transaction would record every allow and silently lose every deny, which is backwards.
This is the opposite of the Cloud's audit log (C18), which *does* share the caller's
transaction: an entry there describes a change, and an entry describing a rolled-back
change would be a lie. An entry here describes a decision, which happened regardless.

**Append-only is a trigger, not a habit.** `bytes` is the single exception and is
write-once: a transfer's size is only known once it has streamed, but the entry has to
exist before it starts, or a connection dropped mid-stream would leave no record that
access was granted at all. The migration's trigger permits exactly one null-to-value
write and refuses everything else — a second `bytes` write, any other column, any delete.
Measured against SQLite before it was written.

That matters for the tests: `SQLModel.metadata.create_all` creates tables, **not**
triggers, so a suite built the usual way would assert append-only against a database that
has no such rule and pass while testing nothing. `tests/harness/migrated.py` exists for
that reason, and `test_access_log.py` is the one suite that runs the real migrations.
Dropping the triggers fails 15 of its tests, which is how we know they are not vacuous.

**The request id is always minted here** and returned as `X-CIRCULess-Request-Id`, never
read from an inbound header. An id the caller chooses can be repeated or collided with
somebody else's, and the one thing this field has to be good for is finding every entry
belonging to one request — during an investigation of that caller.

A few consequences that look odd until you see why:

- A cross-org attempt is recorded against **the tenant it targeted**, not the caller's.
  The organisation entitled to ask "who has been trying my data" is the one whose data it is.
- Reading the log is itself a management decision, so it appears in the log it returns.
- An unknown tenant is **not** logged: there is no organisation the entry could belong to,
  and "someone asked about a tenant we do not host" is a fact about this node rather than
  a decision about anyone's resources.
- Entries survive N20's purge (D25). `resource_id` therefore often names something that no
  longer exists, which is correct — an audit answers what happened, not what is still there.
- **Never a name or an email** (D31). `subject_sub` is Keycloak's UUID; node-audienced
  tokens carry neither claim, and a test asserts the outcome rather than trusting the realm
  setting that produces it.

## Running it on the overlay (N15)

A node ships as a **container image**, pinned by digest and signed with cosign. That
answers the packaging half of O18, and the reason is the overlay:

```
netbird container ──┐
                    ├── one network namespace ── nothing published to the host
node container   ───┘
```

The NetBird agent owns the namespace and the node joins it with
`network_mode: service:netbird`, so the WireGuard interface exists only inside that pair.
The node publishes **no host port**, and **nothing else on the machine is on the
overlay** — a mistaken access rule exposes one container rather than a partner's estate.
Running the agent on the host instead would put every listening service on the machine
within reach of whichever peers the rules allow.

Two structural tests hold that shape: `deploy/docker-compose.yml` must publish no ports,
and the node must keep `network_mode: service:netbird`. Losing the second makes the node
unreachable, and the obvious fix for that is to publish a port.

`deploy/README.md` is the partner install guide. The NetBird server side — groups,
single-use keys, default-deny rules, and keeping BVR's own routes and DNS away from
CIRCULess peers — lives in the `circuless-cloud` repository at
`deploy/netbird/POLICIES.md`.

### Finding its own address

`overlay.py` looks for an address in `100.64.0.0/10`, NetBird's allocation range, by
asking each interface with `ioctl(SIOCGIFADDR)`. No NetBird binary — the agent is in the
*other* container — and no iproute2, which the runtime image deliberately lacks.

That address is what `/.well-known` publishes — as **`http://`**, not `https`. The node
terminates no TLS on either socket, and on the overlay it does not need to: D28 encrypts
that hop with WireGuard and reserves TLS for publicly reachable endpoints, which is what
the gateway URL is for. So a node cannot advertise an address it is not on, nor a scheme
it does not speak. `CIRCULESS_NODE_OVERLAY_BASE_URL` overrides it for what detection cannot know
about, and `check` warns if the override disagrees with the interface.

The first implementation used `getaddrinfo(interface_name)`, which is not a thing: it
fails for every interface on Linux, so detection returned `None` always — and passed
every test, because the development machine has no overlay and `None` is also the right
answer there. `tests/test_overlay.py` carries the container command that caught it.

### Residual exposure, stated plainly

The node binds `0.0.0.0` on its public socket, and the namespace also holds `eth0` on
the Docker bridge, which it needs to reach Keycloak and the Cloud API. On a Linux host
the socket is therefore reachable from the host's bridge and from containers on it.

Every route requires a valid token for this node's audience (D21), so what that reaches
is a 401. Binding to the detected overlay address would remove it, and is deliberately
not done: a node always starts before its peer is approved, so the address does not
exist yet and the bind would fall back to `0.0.0.0` every time. The real fix is
rebinding once the peer connects, which is larger than it looks and is not N15.

### The private key and the image

The key, the certificate, the database and the tenants' content all live in one volume at
`/var/lib/circuless`, owned by uid **10001**. The node refuses to start if anyone else
can read the key (invariant 17), so a named volume — which inherits the image's
ownership — is the default; a bind mount needs `chown 10001:10001` first.

`data/` is in `.dockerignore` for the same reason. It is gitignored, so it never reaches
a commit, but a build context is not a commit.

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
circuless_node_sync_age_seconds      seconds since this process last PULLED, or -1
circuless_node_pull_failures_total   consecutive failed agreement pulls
circuless_node_push_failures_total   consecutive failed catalogue pushes
circuless_node_agreements_cached     agreements currently enforced
```

**Push and pull are tracked separately, and only the pull is staleness.** The pull
carries agreements, which is what `decide()` enforces from; the push carries catalogue
metadata, which only affects what others can discover. Alert on the pull; a rising push
counter is a ticket.

A failed push does not stop the pull. It used to: one record the Cloud would not
accept — or a tenant an operator removed from the node registry — returned before the
pull and left the node enforcing from a cache nobody was updating, indefinitely, while
reporting a failure nobody reads as "enforcement is frozen".

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
    resources.py  N5 registration, the licence and classification rules, the log read
    access_log.py N11 every decision, append-only; the request id middleware
    transfer.py   N8 serving a dataset's bytes, and bucket manifests
    upload.py     N19 receiving them; staged writes and the size limit
    sync.py       N7 push, pull, heartbeat, and the staleness metrics
    well_known.py N12 what the node publishes about itself
    decide.py     N6 who may read or invoke what, and on whose behalf
    management.py N18 who may manage a tenant's resources
    dcat.py       rendering DCAT-AP from typed fields
    vocabularies.py  the controlled lists: licences, themes, classification
    identity.py   the node's keypair, certificate and Cloud credentials
    oidc.py       discovery, shared by the JWKS cache and the credentials
    db.py         engine; SQLite in WAL mode
    storage.py    fsspec adapter, and path confinement
    errors.py     reason codes — one enum, no free text
migrations/       Alembic, from the first model
deploy/           the overlay stack: Dockerfile lives at the root, compose and guide here
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

`CIRCULESS_NODE_MAX_UPLOAD_BYTES` is the largest single upload accepted (N19), 1 GiB by
default.

`CIRCULESS_NODE_CORS_ALLOW_ORIGINS` is an exact list and the node **refuses to start** on
`"*"` (G7). Browser downloads through the gateway are cross-origin and carry a bearer token,
so a wildcard would let any site spend a user's node token.

## State of the build

**M1, complete:** **N1** skeleton, **N14** storage, **H3**'s `/v1` prefix, **Q1** test
harness, **N2** token verification, **N3** subject resolver, **N4** tenancy, **N17** node
self-authentication.

**M2, complete:** **N5** resource registry, **N18** management authorization, **N7**
sync client, **N12** `/.well-known/circuless-node`, **N15** the image, the overlay stack,
the install guide and overlay address detection, and the remote dry run (Q3) on a machine
outside BVR's network.

**M3, in progress:** **N6** `decide()`, **N11** access log, **N8** data transfer and
H3's path confinement, **N19** upload.

Next: N20 two-stage deletion, then N10 credentials and N9 proxy.

Not yet built, and deliberately absent rather than stubbed: uploads and deletion. Deletion is two-stage (D25, N20 in M3), so there is no `DELETE` at all — a
placeholder that actually removed a row would be the wrong thing to have to take back.
`ResourceStatus.WITHDRAWN` exists from the start, and every query already excludes it, so
N20 does not have to find the one that forgot.
