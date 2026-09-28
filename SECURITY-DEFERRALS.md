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
