# CIRCULess Node

The CIRCULess Node is the data plane of the CIRCULess core platform. It holds organisations' datasets, fronts their services, and **decides every access locally**. One process hosts several tenants (organisations). It runs in the Cloud or on a partner's premises, and reaches the rest of the platform over a NetBird/WireGuard overlay with no inbound ports.

Target: **TRL 5 beta**, built 28 Sep – 23 Oct 2026. Correctness and the security invariants below matter more than features or performance.

## Where the design lives

The architecture repo is checked out at `docs/architecture/` (git submodule). Read the relevant file before changing behaviour; do not load them all at once.

| Need | File |
|---|---|
| Why the node works this way; decisions D1–D35 | `docs/architecture/working/Node_Identity_design_draft.md` §3–§5 |
| Component IDs (N1–N20, H1–H5) and what each does | `docs/architecture/working/Node_Identity_build_breakdown.md` §2.2, §5 |
| Milestones, tasks, exit criteria, test cases T01–T35 | `docs/architecture/working/Node_Identity_implementation_plan.md` |
| Requirements (FR/NFR) | `docs/architecture/D2.1_requirements.md` |
| Security requirements (`SR-…`) and how each is verified | `docs/architecture/security_requirements_review.md` §4 |
| Terms | `docs/architecture/12_glossary.md` |

If code and design disagree, stop and ask. Do not quietly change either one.

## Stack

Python 3.12 · **uv** (commit `uv.lock`) · FastAPI + Uvicorn · SQLModel/SQLAlchemy on **SQLite (WAL)**, Postgres via `DATABASE_URL` · **Alembic from the first model** · `pyjwt[crypto]` + `httpx` · `private_key_jwt` (authlib or hand-rolled) · `fsspec`, file storage only in the beta · `cryptography` (Fernet) · Jinja + htmx for the admin UI, no JS build step.

Distributed as `uvx circuless-node==<version>`, always pinned, and as a non-root container image pinned by digest.

## Security invariants — never break these

Each one has a test. A change that weakens one needs J's explicit approval.

1. **`/v1` prefix on every API route** (D24). Only these live outside it:
   - `/.well-known/circuless-node` — requires a platform token;
   - `/healthz` — overlay or internal interface only, status code, no body;
   - `/metrics` and `POST /internal/authz` — internal or overlay interface only.
2. **No anonymous routes** (D21). Every route requires a valid token. A CI test lists every FastAPI route and asserts that a request without a token gets 401. New routes are covered automatically — never exempt one.
3. **Token checks, in this order** (N2):
   - signature against the cached JWKS — on an unknown `kid`, refetch **once**, rate-limited;
   - `iss`;
   - `aud` must be exactly `node:{this node_id}`;
   - `exp` / `nbf`, with 60 s leeway.
4. **Node tokens are refused** (D14). `principal_type=node` → 403 `node_principal_not_permitted`, on every consumption *and* management endpoint.
5. **Identity comes from the token, never from the request body** (R3, D17).
   - Orgs: users' come only from **depth-one** `/orgs/<x>` groups; services' from the `org_id` claim. Ignore every other group.
   - `/orgs/<x>/admins` makes the user admin of X only.
   - `org_ids` is a set.
   - The actor is `azp`.
6. **Acting org** (R11). Intersect the subject's orgs with the orgs that could authorise the request:
   - exactly one → that one;
   - several → require `X-CIRCULess-Acting-Org`, checked against membership;
   - otherwise deny `ambiguous_acting_org`.
7. **`decide(subject, action, resource, agreements, now)` is pure** (D1). No I/O, no clock, no DB. Agreements and time are passed in, and it returns a decision and a reason code. It is the **only** place that grants access; handlers never re-implement a rule. Actions are `read` and `invoke`.

   | Resource state / visibility | Allowed |
   |---|---|
   | `withdrawn` (any visibility) | nobody |
   | `private` | admins of the owning org |
   | `org` | any user or service of the owning org |
   | `agreement` | the owning org, plus an **accepted** agreement matching provider, acting consumer org, resource (or all of the provider's), action and `[valid_from, valid_until)` |
   | `public` | any authenticated user or service — never anonymous, never a node |

   The forward-auth shape (`/internal/authz`) is an early reject only. It never writes an AccessLog entry, and the in-process check always runs as well (R7).
8. **Tenancy** (R10).
   - Tenant-owned tables carry `tenant_id` and are filtered once, centrally, with `with_loader_criteria`.
   - Node-global tables — `AgreementCache`, `OrgMap`, `NodeIdentity` — are explicitly excluded.
   - Never hand-write a tenant filter in a handler.
9. **Path confinement** on `/data/{path}` and `/invoke/{path}` (SR-3.2.3). Normalise the path, then refuse:
   - `..`;
   - absolute paths;
   - scheme-relative or absolute URLs.
10. **Service proxy** (N9, R5, R6, R13).
    - **Strip** from the caller: `Authorization`, `Cookie`, `Host`, hop-by-hop headers, and **any inbound `X-CIRCULess-*`**.
    - **Inject** the upstream credential and `X-CIRCULess-Subject`, `-Org`, `-Resource`, `-Request-Id`, `-Actor`.
    - **Forward** only the allowlist: `Content-Type`, `Accept`, `Content-Length`, `Idempotency-Key`, and — for streaming resources only — `Mcp-Session-Id`, `Mcp-Protocol-Version`, `Last-Event-ID`.
    - Rewrite upstream `Location` headers to the node's `/invoke` URL; refuse redirects anywhere else.
    - Stream SSE unbuffered, with `stream_timeout_s`.
    - Pass async `202` responses through. The node holds no job state.
11. **ServiceCredential** (N10, N18).
    - Fernet-encrypted at rest.
    - Set or rotated by **org admins only**, never by a service principal.
    - Never returned by any API or UI.
12. **AccessLog** (N11). Every decision gets an entry — allow and deny, consumption and management.
    - Fields: request id, tenant, resource, action, pseudonymous `sub`, principal type, actor (`azp`), acting org, decision, reason, bytes.
    - Append-only.
    - **Never store names or emails.** Node tokens don't carry them (D31).
13. **Defaults and publishing.**
    - New resources are `discoverability=hidden`, `visibility=org` (NFR4).
    - A licence (`dct:license`, controlled list) is required before publishing to the catalogue (NFR9).
    - Every dataset has `classification ∈ {synthetic, non-sensitive, sensitive}`. A **BVR-operated node refuses `sensitive`** (D22).
14. **Deletion is two-stage** (N20, D25). `DELETE` marks the resource `withdrawn`: `decide()` denies it and the catalogue record is withdrawn. A purge job removes data and metadata after `purge_after`. AccessLog entries stay.
15. **Errors.**
    - Status code plus a reason code from **one enum**. No stack traces, internal paths or versions.
    - Upstream error bodies are passed through only on `/invoke`.
    - Known codes: `node_principal_not_permitted`, `no_agreement`, `ambiguous_acting_org`. Add new ones to the enum, never as free text.
16. **Identifiers are UUIDs.** Never use sequential integers in anything external.
17. **Key material** (N17).
    - Keypair generated on first start and wrapped in a **self-signed X.509 certificate**. The private key never leaves the host.
    - The private key and the Fernet key are files with mode `0600`, owned by the node's service user.
    - **No secrets in git, ever.** Gitleaks runs in pre-commit and CI.
18. **CORS**: explicit origin allowlist from settings, never `*`.

## API surface (design §4.4)

```
# Management — authorised by management rules (N18), logged
POST   /v1/t/{tenant}/resources                   register dataset or service
GET    /v1/t/{tenant}/resources                   list, filtered by caller
GET    /v1/t/{tenant}/resources/{id}              DCAT metadata
PATCH  /v1/t/{tenant}/resources/{id}
DELETE /v1/t/{tenant}/resources/{id}              two-stage
PUT    /v1/t/{tenant}/resources/{id}/data         upload file (size limit)
PUT    /v1/t/{tenant}/resources/{id}/data/{path}  upload one bucket object
PUT    /v1/t/{tenant}/resources/{id}/credential   set / rotate ServiceCredential — org admins only
GET    /v1/t/{tenant}/access-log                  org admins of T, platform-admin

# Consumption — authorised by decide()
GET    /v1/t/{tenant}/resources/{id}/data         file bytes (Range) or bucket manifest
GET    /v1/t/{tenant}/resources/{id}/data/{path}  one bucket object, decided per object
ANY    /v1/t/{tenant}/resources/{id}/invoke/{path} proxied service call
```

**Management rules** (N18):

| Operation | Who |
|---|---|
| Register, update, upload, delete | Admins or service principals of the tenant's org |
| Credentials | Admins only |
| Read the log | Admins and `platform-admin` |
| Node configuration | Node client role `admin` |

**Sync** (N7):
- Push the DCAT record per tenant on every change.
- Every 30 s, pull the provider-side agreements and the org map from the Cloud API.
- Keep enforcing from the cache if the Cloud is down, and report staleness on `/healthz` and `/metrics`.

**`invoke_policy`**, set per service resource: `timeout_s`, `stream_timeout_s`, `async`, `idempotent_methods`, `max_request_bytes`, `streaming`.

## Working rules

- **Tests first for security behaviour.**
  - `decide()` gets exhaustive table tests: every visibility × principal type × agreement state × time boundary, plus `withdrawn`.
  - Integration tests run against a **real Keycloak in Docker Compose**, never a mocked JWT issuer.
  - Security-critical acceptance cases (T02–T04, T07, T08, T11, T13, T17, T19, T23, T25, T28, T32, T34) must never be skipped.
- **Git.** BVR's CI tool enforces branch naming, commit messages and tags. Commits are **signed** (SSH signing). `main` is protected: one required review, no direct pushes.
- **Dependencies.** Pinned in `uv.lock`, images pinned by digest. A new dependency needs a one-line reason in the PR (SR-6.1.2). Prefer the standard library and the stack above.
- **Before calling a task done:** tests green, the route-auth test green, and no new unauthenticated or un-versioned route.
- Keep it small. The node is a dozen-odd endpoints and one decision function. Resist frameworks and abstractions it does not need.

## Current milestone plan (node side)

| Milestone | Due | Node work |
|---|---|---|
| M1 | Fri 2 Oct | N1 skeleton (settings, Alembic, CORS, interface binding) · N14 storage · N2 token verification · N3 subject resolver · N4 tenancy · N17 self-authentication · `/v1` · test harness with real Keycloak |
| M2 | Fri 9 Oct | N5 resource registry · N18 management authz · N7 sync client · N12 `/.well-known` · N15 NetBird join · UUIDs and classification guard · dry run on a BVR machine outside BVR's network |
| M3 | Fri 16 Oct | N6 `decide()` · N8 transfer · N19 upload · N11 AccessLog · N20 two-stage deletion · errors and path confinement · N9 proxy · N10 credentials |
| M4 | Thu 22 Oct | N13 admin UI · N16 packaging (pinned `uvx`, non-root image, signed) · deployments: BVR cloud, BVR on-prem, BVR external |
| M5 | Fri 23 Oct | Two-organisation acceptance test, 34 cases |

**Pending input.** The token-exchange spike (Q2, M1 day 2) confirms the claim shape of exchanged tokens. Finalise N2 and N3 only after it.
