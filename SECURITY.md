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

## Reporting a vulnerability

<!-- Add your responsible-disclosure contact / process here (e.g. security@bavenir.eu). -->
