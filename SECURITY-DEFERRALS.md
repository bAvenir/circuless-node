# Deferred security controls

Controls the design requires that are **not built yet**, recorded here rather than left
implicit. CI prints this file on every run, so a deferral cannot quietly become an
omission between now and the audit.

Implementation plan §8: by the security validation, every mandatory requirement is either
**verified**, or **covered by an exception J has accepted in writing**. Both apply to
everything below.

> **Reclassified 2 November 2026 (J).** The first two rows — SR-6.1.4 and SR-4.3.5 — are
> now **recommended for the beta and enforced from the first release after it**, rather
> than mandatory and due 4 November. Their rows stay here because they are still open and
> CI prints this file on every run. The other two rows are unaffected.

| Control | Requirement | Due | Status |
|---|---|---|---|
| Signed commits and signed release tags | SR-6.1.4, H1 | after the beta | **recommended for the beta** — enforce after it |
| Branch protection with one required review | SR-4.3.5, H1 | after the beta | **recommended for the beta**; mandatory once a second developer joins |
| Server-sent events demonstrated against a real client | N9, invariant 10, T20 | — | **deviation** (J, 3 Nov 2026) — not demonstrated for the beta |
| The reference service in place of the named pilot services | X3, X4 | — | **deviation**, accepted |

## Signed commits and tags

**Why deferred.** No signing key is configured and the whole history is unsigned, so
enforcement would have to start from a baseline anyway. Setting it up needs a key
registered against a GitHub account — a step for a person, not for CI.

**What it costs.** Half of M1's exit criterion *"CI fails on … an unsigned commit"*. The
other half — planted secrets, critical CVEs, unauthenticated routes — is enforced. Since
2 November that half is **out of scope for the beta by decision** rather than
outstanding; the implementation plan's checklist records it the same way.

**What closing it looks like.** Set `commit.gpgsign`, `tag.gpgsign`, `gpg.format=ssh` and
`user.signingkey`; register the public key on GitHub as a *signing* key. `bvr-ci` needs no
change: it runs plain `git commit` and `git tag -a`, both of which sign automatically once
those are set. The CI check must verify only commits **new in a pull request**, since
existing history is unsigned.

## Branch protection with one required review

**Why deferred.** There is one developer. A required review would block every merge, and
self-approval through a second account is theatre rather than review.

**What it costs.** Nothing that CI enforces — the gates run on every push regardless. What
is missing is the guarantee that they were *seen*.

**What closing it looks like.** Protect `main` against force-push and deletion, require CI
to pass, and add the review requirement when there is a second person to do it.

---

Realm-level deferrals — MFA for `platform-admin`, and disabling the password and implicit
grants — are recorded separately in `circuless-cloud`, in `deploy/keycloak/realm-config.json`
under `deferred`, and printed by `kc.py verify`.


## Server-sent events — supported, not demonstrated

> **Reclassified 3 November 2026 (J): a deviation, not a deferral.** It had carried a
> due date of 23 October and sat unmoved. Acceptance case **T20** is the case that would
> have demonstrated it, and T20 is now recorded as not run in
> `circuless-cloud/deploy/acceptance/RUN-SHEET.md` for this reason.
>
> What is missing is a demonstration, not the capability: the proxy returns a
> `StreamingResponse` and the gateway is required by cloud invariant 17 to stream
> unbuffered. Neither is evidence. Nothing in either repository exercises an SSE
> endpoint — the reference service has none, and no test asserts that a first event
> arrives before a stream completes. Closing it needs an SSE endpoint on the reference
> service and a client that measures arrival order, which is the work, not the
> verification.

Not a missing control and not a deferred one: the node implements it. `invoke` relays a
streaming response unbuffered and forwards `Mcp-Session-Id`, `Mcp-Protocol-Version` and
`Last-Event-ID` for a resource registered `streaming: true`, under `stream_timeout_s`
rather than `timeout_s`. Unit tests cover the header handling and the policy.

**What is missing is evidence against a real client.** The reference service
(`examples/reference-service/`) exercises the synchronous path, the error path,
redirects and the asynchronous `202`, all against a real server — but not SSE. Nothing
has yet held a stream open through a node and read events off it.

That matters because streaming is where a proxy's defects show: buffering that is
invisible on a 200-byte JSON body makes an event stream useless, and the node's own
`access_log` middleware is pure ASGI precisely to avoid that class of problem.

**Recorded now rather than at the audit**, so the claim made about streaming is bounded:
it is implemented and unit-tested, not integration-tested. A partner integrating a
streaming service in the pilot phase will be the first real exercise of it, and should
be told so.

## The reference service in place of the named services — a deviation

The build breakdown listed **X3** and **X4** as integrations of named pilot services.
Neither was available to integrate against in October.

**What was built instead.** A reference service (`examples/reference-service/`), its
integration contract (`INTEGRATING-A-SERVICE.md`) and a browser application
(`examples/browser-app/`), together exercising the full consumption path against a real
HTTP server: a cross-organisation agreement, the credential the node holds, the proxy,
and the access log on both halves of an exchange.

**Why this is not a reduction in scope.** The three M3 exit criteria that named a
service are better stated as properties, and are met:

| was | is |
|---|---|
| service X is reachable through a node | *a service is reachable through a node, authorised by an agreement between two organisations, and both halves of the exchange are logged* |
| service X authenticates to the node | *the node holds and injects the service's credential; the service authenticates the node and nothing else* |
| service X is integrated | *the contract a service must meet is written, and a worked example meets it at two levels of integration* |

It is also stronger in one respect: the example proves a service that has **never heard
of CIRCULess** works unmodified, which an integration with a service built for the
project would not have shown.

**What it does not give us.** No evidence from a partner's real system, their real
network, or their real data. That is the partner integration phase (D35), and TRL 5
relevant-environment evidence was always going to come from there rather than from M5.

**Found by doing it.** The exercise turned up a real defect in the node: redirect
`Location` rewriting used the tenant's identifier where the route expects its slug, so
every rewritten redirect was a URL the caller could not follow. The unit test covering
it compared the rewritten string against an expectation written from the same mistaken
code. A test that followed the URL found it at once. That is the argument for this
deviation in one sentence.
