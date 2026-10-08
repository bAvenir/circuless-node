# Deferred security controls

Controls the design requires that are **not built yet**, recorded here rather than left
implicit. CI prints this file on every run, so a deferral cannot quietly become an
omission between now and the audit.

Implementation plan §8: by the security validation, every mandatory requirement is either
**verified**, or **covered by an exception J has accepted in writing**. Both apply to
everything below.

| Control | Requirement | Due | Status |
|---|---|---|---|
| Signed commits and signed release tags | SR-4.3.x, H1 | 4 Nov 2026 | deferred |
| Branch protection with one required review | H1 | 4 Nov 2026 | deferred |
| Server-sent events demonstrated against a real client | N9, invariant 10 | 23 Oct 2026 | **not demonstrated** |
| The reference service in place of the named pilot services | X3, X4 | — | **deviation**, accepted |

## Signed commits and tags

**Why deferred.** No signing key is configured and the whole history is unsigned, so
enforcement would have to start from a baseline anyway. Setting it up needs a key
registered against a GitHub account — a step for a person, not for CI.

**What it costs.** Half of M1's exit criterion *"CI fails on … an unsigned commit"*. The
other half — planted secrets, critical CVEs, unauthenticated routes — is enforced.

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
