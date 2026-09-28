# The claim contract

The node does not read the CIRCULess realm definition — that lives in `circuless-cloud`, a
private repository, and this one is public and Apache-2.0. Coupling a public repo's CI to a
private one would mean a secret in public settings and broken CI on every fork PR.

Instead the two sides meet at a **contract**, and each tests its own half:

| Side | Tests | Where |
|---|---|---|
| Cloud | that the realm **emits** this shape | `kc.py verify` in `circuless-cloud` |
| Node | that it **consumes** this shape correctly | this test suite |

`tests/realm/test-realm.json` is a minimal realm carrying exactly the claims below, and
nothing else. It is a fixture, not a copy of the production realm.

**If you change anything here, change it on both sides.** The cloud repo's
`deploy/keycloak/README.md` points back at this file.

## What a token carries

| Claim | Source | Node relies on |
|---|---|---|
| `sub` | the subject | Pseudonymous identity. Logged; never resolved to a name on the node (D31, NFR12) |
| `aud` | the requested audience scope | **Exactly** `node:{node_id}`. Any other audience, or a second node audience, is rejected |
| `iss` | the realm | Must match the configured issuer |
| `groups` | Group Membership mapper, **full path** | Orgs are depth-one under `/orgs`. `/orgs/x/admins` makes the subject an admin of x alone. Everything outside `/orgs` is ignored (G14) |
| `principal_type` | User Attribute mapper on the subject | `user`, `service` or `node`. **`node` is refused** at every consumption and management endpoint (D14) |
| `org_id` | User Attribute mapper on the subject | A service principal's organisation. Absent for users — their orgs come from `groups` |
| `node_id` | User Attribute mapper on the subject | Present on node principals only |
| `azp` | the client the token was issued to | The **actor**. For a service acting for a user, this is the only trace of the service — there is no `act` claim |

## Properties, not just field names

These are what the node's behaviour actually depends on:

1. **Identity comes from the subject, never the client.** A hardcoded client claim would
   apply to every token that client obtains, including ones it gets by exchange, putting a
   service's `org_id` on a user's token (R1, D17). The cloud asserts the realm has no
   hardcoded claim mappers at all.
2. **`groups` is full-path.** `/orgs/alpha`, not `alpha`. The resolver keys on the path.
3. **One audience per token.** A token audienced for two nodes is replayable between them,
   so the node requires its own audience and no other node's (§5.4).
4. **Node-audienced tokens carry no `name` or `email`** (D31). This is a *client* contract,
   not something the realm can enforce — `profile` and `email` are assigned per client, not
   per audience — so the cloud keeps them optional on clients that can request a node
   audience, and T01 checks it.
5. **`principal_type` may be absent.** Keycloak's User Profile has no attribute defaults, so
   an unset attribute produces no claim. The node **fails closed**: a token with no
   `principal_type` is refused rather than treated as a user, because a node whose attribute
   was never set would otherwise pass the check meant to reject it.

## Open: what an organisation is called

Today an organisation is identified by a **slug** on both sides — `alpha`, read from
`/orgs/alpha` for a user, and whatever the `org_id` attribute holds for a service. The
fixture realm and the production realm currently agree on that.

§3.2 says the Cloud's registry maps `/orgs/alpha` to a **UUID** and syncs it to nodes, at
which point both should carry UUIDs instead. The translation belongs in N7, where OrgMap
arrives.

**Both sources have to move together.** If users resolved to slugs while services resolved
to UUIDs, every comparison between them would silently fail to match — a service would
simply never be in the same organisation as a user, and no error would say so. Whoever
does N7 changes the group-path translation and the `org_id` claim in the same change.

## Deliberately not here

The test realm has no `circuless-cloud` audience, no service-audience scopes (`svc:*`), no
`platform-admin` role and no token exchange. Those matter to the Cloud API and to services,
not to what the node parses out of a token. They arrive here only if a node component starts
depending on them.
