# Security

## Verifying a release

Every image released from this repo since `2026-09-24` is signed with
[Cosign](https://docs.sigstore.dev/cosign/overview/). The current public key is committed at
[`cosign.pub`](./cosign.pub) — it is not a secret, and you don't need anything from us beyond this
repo to check a release yourself:

```bash
cosign verify --key cosign.pub <registry>/<image>@<digest>
```

A successful verify confirms the image was built and pushed by someone holding this project's
private signing key — it does not by itself mean the image is vulnerability-free; see the
release's SBOM and scan results for that (tracked separately, see the roadmap).

## Key history

If the signing key is ever rotated (e.g. a team member with access leaves), the retired public key
is kept under a dated name so older releases stay verifiable — never delete an old key.

| Key file | Active | Notes |
|---|---|---|
| `cosign.pub` | `2026-09-24` – present | current signing key |

## Dependency advisories

Findings from the `trivy_scan` gate on `uv.lock`, and what was done about each. Newest
first. The gate fails on HIGH and CRITICAL.

| Date | Advisory | Package | Severity | Resolution |
|---|---|---|---|---|
| 22 Oct 2026 | [GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2) | `certifi` | CRITICAL | `2026.7.22` → `2026.10.19` |

### GHSA-6q48-6c7w-9gw2 — a root CA with compromised intermediate keys

`certifi` 2026.7.22 ships **emSign ECC Root CA - G3** in its bundle, a root whose
intermediate CA keys were compromised. A trust-store problem rather than a code defect:
nothing in `certifi` runs, and no input reaches it. What it means is that TLS verified
against that bundle would accept a certificate issued under that root — and with the
intermediate keys in someone else's hands, that is a certificate for a host they do not
own.

`certifi` is transitive here, through the node's only HTTP client:

```
httpx -> httpcore -> certifi
```

**Where a node is exposed.** Every outbound TLS request the node makes verifies against
this bundle, and there are three kinds — more, and wider, than the Cloud's single
issuer:

- **Keycloak**, for OIDC discovery, the JWKS, and the node's own `private_key_jwt`
  authentication (`jwks.py`, `oidc.py`, `identity.py`). The JWKS is the signature
  authority for every token the node accepts — invariant 3 — so a substituted key set
  is a substituted identity model;
- **the Cloud API**, which the sync client (N7) polls every 30 s for provider-side
  agreements and the org map (`sync.py`). The agreement cache is what `decide()` reads,
  so a substituted response is a substituted authorization input;
- **upstream services**, called by the proxy (N9) at addresses a tenant admin configures
  (`proxy.py`). Unlike the first two, that is not a known host — it is whatever any
  tenant has registered, and the node injects the upstream credential into each call.

Both are reasons this was worth fixing promptly rather than at the next routine bump.

**The fix.**

```sh
uv lock --upgrade-package certifi
```

Not `uv add certifi@2026.10.19`: that would promote a transitive dependency to a declared
one and leave this repository pinning someone else's trust store indefinitely. The lock
moves and `pyproject.toml` does not change.

Verified: Trivy reports 0 vulnerabilities with and without `--include-dev-deps`; 1271
tests pass and 22 skip, the usual platform skips on a macOS checkout rather than
anything this change caused; and the bundle lost **emSign ECC Root CA - G3** and nothing else, 121
certificates down to 120. That last check matters — the remedy for a bad root is to
delete it, so a `certifi` upgrade reduces what the node will trust, and the failure mode
is a host that stops verifying rather than one that newly does. Three other emSign roots
remain trusted and are not part of this advisory.

**Not done by the lockfile change.** The published image installs with `uv sync
--frozen`, so it carries whatever the lock said when it was built. The fix reaches a
deployed node only on a rebuild, a cosign signature and a new pinned digest.

## Reporting a vulnerability

<!-- Add your responsible-disclosure contact / process here (e.g. security@bavenir.eu). -->
