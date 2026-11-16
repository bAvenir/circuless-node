# Connecting the DPP to CIRCULess

The **DPP Adapter** is a CIRCULess service that receives events from the DPP and hands
them to Workflows exactly once per acknowledgement. This is how the DPP reaches it.

Everything here is an instance of [`INTEGRATING-A-SERVICE.md`](../../INTEGRATING-A-SERVICE.md)
— §8 for the registration, §11 pattern B for the token. Nothing below is a special case;
if it reads like one, something has been set up wrong.

## Two paths, and you are on the first

The DPP publishes to the **same route, with the same body, for the same answers**, either
way. Only the address and the credential differ:

| | address | credential | available |
|---|---|---|---|
| **A — direct** | `https://$INGEST_HOST/v1/events` | a fixed bearer token | now |
| **B — through a node** | `https://$NODE_HOST/v1/t/$TENANT/resources/$RESOURCE/invoke/events` | an OAuth token the DPP mints itself | once `svc-dpp` exists in the realm |

**Start with A.** It exists so integration can begin before the identity work is done,
and moving to B later is a change of base URL and `Authorization` header in the DPP's
client — nothing else.

Be clear-eyed about what A costs while it lasts: it bypasses the node, so those pushes
appear in **no access log**, no agreement governs them, and the token has no expiry. That
is a deliberate, temporary trade, not the design.

Part 1 is needed for both. Part 2 is path A. Part 3 is path B.

## Settings used throughout

```sh
NODE_ID=bvr-cloud                                   # the node hosting the adapter
NODE_HOST=$NODE_ID.nodes.<your-domain>
INGEST_HOST=dpp-ingest.<your-domain>                # path A only
ISSUER=https://auth.<your-domain>/realms/circuless
TENANT=bvr                                          # the tenant that owns the adapter
```

> `NODE_ID` is a placeholder. Substitute the node that actually hosts the adapter — the
> hostname and the token audience both derive from it, so getting it wrong fails twice
> and in two different ways.

---

# Part 1 — the organisation running the adapter

## 1.1 Run the adapter beside the node

[`../add-your-service`](../add-your-service) is the pattern. The adapter's node-facing
container gets **no host port**: nothing but the node can reach it, which is why it may
trust the `X-CIRCULess-*` headers at all.

Its published container — path A — is the one exception, and it serves exactly one route.

## 1.2 Register it as a service resource

The adapter ships `deploy/register-on-node.sh`, which does this and the credential below:

```sh
NODE=https://$NODE_HOST TENANT=$TENANT TOKEN=<a token for this node> \
  ./deploy/register-on-node.sh
```

It prints the resource id. Keep it: that is `$RESOURCE`, and path B needs it.

By hand, the same call is:

```sh
curl -sX POST https://$NODE_HOST/v1/t/$TENANT/resources \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"slug":"dpp-adapter","kind":"service","title":"DPP Adapter",
       "theme":"value-chain","classification":"non-sensitive",
       "visibility":"agreement","discoverability":"hidden",
       "endpoint_url":"http://dpp-adapter:8000/v1",
       "invoke_policy":{"idempotent_methods":["GET","POST"],"timeout_s":30}}'
```

- `endpoint_url` carries the `/v1`, so the adapter's own paths stay as it publishes them.
  Without it, `invoke/events` would reach `/events`, which the adapter does not serve.
- `visibility: agreement` because the DPP is a different organisation, so an agreement
  governs the access and can be revoked. Use `org` only if every caller belongs to the
  organisation owning this tenant — convenient, and worth being deliberate about, since
  there is then nothing to revoke.
- `discoverability: hidden`: this is plumbing between named parties, not a catalogue entry.
- `idempotent_methods` is an **allowlist of methods, not a retry list** — the node never
  retries. `GET, POST` is all this adapter needs; leaving the others out means a caller
  cannot reach them even by guessing a path.
- Loopback, `0.0.0.0` and link-local are refused at registration and again on every call.
  A container name on the node's private network is the intended shape.

**This one resource serves three routes.** `POST invoke/events` is the DPP's. Workflows
also uses `GET invoke/events` to lease events and `POST invoke/events/ack` to acknowledge
them. The node cannot tell them apart — its decision is per resource and per action, so
everyone permitted to `invoke` can reach all three — and the adapter is what keeps the
DPP to publishing and Workflows to consuming.

## 1.3 Give the node the adapter's key

```sh
curl -sX PUT https://$NODE_HOST/v1/t/$TENANT/resources/$RESOURCE/credential \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"scheme":"header","header_name":"X-API-Key","secret":"<the adapter's node key>"}'
```

`X-API-Key` is the header the adapter checks, and the same value it reads from its own
`DPP_ADAPTER_NODE_KEY_FILE`. A mismatch means the node calls with a credential the adapter
ignores, and every call is refused with a `401` that looks like a node fault.

Stored encrypted, returned by no interface afterwards (invariant 11). To change it, set a
new one.

## 1.4 Permit the DPP, and tell the adapter who it is

**Two separate permissions**, and both are needed:

**An agreement on the node**, from `$TENANT` to the DPP's organisation, on this resource,
action **`invoke`**. One granting `read` instead refuses with `no_agreement` — which reads
like no agreement at all rather than like the wrong action.

**The adapter's own allowlist**, `DPP_ADAPTER_PUBLISHERS=svc-dpp`. Default deny: without
it the DPP is refused with `actor_not_permitted` even holding a perfectly good token and a
valid agreement. Workflows goes in `DPP_ADAPTER_CONSUMERS` and nowhere else.

---

# Part 2 — the DPP, today

Hand this part over with three values: `$INGEST_HOST`, the bearer token, and the event
contract below.

```sh
curl -sX POST https://$INGEST_HOST/v1/events \
  -H "Authorization: Bearer $INGEST_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"id":"dpp-evt-0001","type":"dpp.invoice.updated",
       "occurred_at":"2026-11-15T10:00:00Z","payload":{}}'
```

That is the whole integration. The adapter also serves its own schema at
`https://$INGEST_HOST/v1/openapi.json`, with no credential — that document is the
authority, and this one is a description of it.

The token is a fixed string with no expiry and no scope. Rotating it does not need a
restart, but write the new value to a temporary file and `rename` it into place: a
truncate-then-write leaves a window in which the file is empty, and an empty secret is
refused, so every push in that window fails.

---

# Part 3 — the DPP, through a node

Same route, same body, same answers. What changes is who vouches for the caller.

## 3.1 The operator declares the client

Add `svc-dpp` to `clients` in `circuless-cloud/deploy/keycloak/realm-config.json`,
modelled on `svc-workflows`:

```json
{
  "clientId": "svc-dpp",
  "name": "DPP event publisher",
  "description": "The DPP, as a service principal. Publishes to the DPP Adapter; pattern B only.",
  "enabled": true,
  "publicClient": false,
  "standardFlowEnabled": false,
  "implicitFlowEnabled": false,
  "directAccessGrantsEnabled": false,
  "serviceAccountsEnabled": true,
  "clientAuthenticatorType": "client-jwt",
  "attributes": {
    "use.jwks.url": "false",
    "token.endpoint.auth.signing.alg": "RS256"
  },
  "defaultClientScopes": ["basic", "circuless-identity"],
  "optionalClientScopes": [],
  "audiences": "all",
  "serviceAccountAttributes": {
    "principal_type": "service",
    "org_id": "dpp"
  }
}
```

Two fields decide the security of this integration:

| field | what turns on it |
|---|---|
| `org_id` | the organisation the node decides *against*. The partner's own org means an agreement is required and the access log names them as a third party; `bvr` means there is no agreement to revoke. |
| `audiences` | `all` lets `svc-dpp` mint a token for any node. Narrowing it to the one node costs nothing, since the DPP calls exactly one. |

Note what is **absent**: `standard.token.exchange.enabled`. The DPP acts only as itself —
it has no person to act for — and withholding exchange means it can never acquire one.

Then `kc.py apply --url https://auth.<your-domain>`.

## 3.2 The DPP generates a keypair, once

```sh
uv run act.py keypair --client svc-dpp --key dpp.pem > dpp.pem.crt
```

([`../acting-for-a-user/act.py`](../acting-for-a-user/act.py))

The private key is written `0600` and **never leaves the machine**. Send `dpp.pem.crt` to
the operator; nothing in it is secret. They register it:

```sh
kc.py register-cert --client svc-dpp --cert dpp.pem.crt --url https://auth.<your-domain>
```

**No client secret is issued, in either direction.** That is `private_key_jwt` (D18) — the
same arrangement every node and service here uses.

## 3.3 The DPP mints a token, per call

A JWT signed with that key, presented as a client assertion:

| claim | value |
|---|---|
| `iss`, `sub` | `svc-dpp` |
| `aud` | **the token endpoint** — `$ISSUER/protocol/openid-connect/token` |
| `jti` | unique per assertion |
| `iat`, `exp` | one minute is ample |

Posted as:

```
grant_type=client_credentials
client_id=svc-dpp
client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
client_assertion=<the signed JWT>
scope=node:$NODE_ID
```

`as_itself()` in `act.py` is about fifteen lines doing exactly this, in whatever language
the DPP is written in.

Three rules that are not negotiable:

- **One audience per token.** A token carrying two is replayable between the systems that
  accept it, and every API here rejects it. `scope` names exactly one node.
- **The assertion's `aud` is the token endpoint, not the node.** Getting this wrong
  produces `invalid_client`, which reads like an unregistered certificate and sends you
  looking in the wrong place.
- **Access tokens last five minutes.** Mint per call, or cache with a margin.

## 3.4 The DPP pushes an event

```sh
curl -sX POST https://$NODE_HOST/v1/t/$TENANT/resources/$RESOURCE/invoke/events \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"id":"dpp-evt-0001","type":"dpp.invoice.updated",
       "occurred_at":"2026-11-15T10:00:00Z","payload":{}}'
```

With `act.py` instead of curl:

```sh
uv run act.py as-itself \
  --issuer $ISSUER --client svc-dpp --key dpp.pem \
  --node https://$NODE_HOST --audience node:$NODE_ID \
  --tenant $TENANT --resource $RESOURCE \
  --invoke events --method POST --body @event.json
```

`--no-call` stops after the token and prints its claims. When an integration is not
working, that is the first thing to look at.

> `act.py` is a reference, not a production client: key on disk, token to stdout. Fine for
> proving the flow; not how the DPP should hold its credentials.

---

## The event

Identical on both paths.

```json
{
  "id":          "dpp-evt-0001",
  "type":        "dpp.invoice.updated",
  "occurred_at": "2026-11-15T10:00:00Z",
  "payload":     { }
}
```

All four fields are required; `payload` may be `{}` but not absent. `id` and `type` are
1–200 characters. **Unknown fields are refused, not ignored** — a typo or a version
mismatch, either way worth hearing about rather than storing an event missing something
the sender believed it had sent.

`occurred_at` **must carry a timezone**. A naive timestamp is refused, because it would
otherwise be read in the server's zone and the same string sent from two places would mean
two different instants with nothing to complain about it.

| outcome | status | body |
|---|---|---|
| stored | `202` | `{"id": "<ours>", "duplicate": false}` |
| already held | `200` | `{"id": "<the original>", "duplicate": true}` |

**Deduplication is on `id`** — the DPP's own — and on nothing else. A repeat of an id
already held is not an error and creates no second event: the DPP retrying an uncertain
delivery is correct behaviour, and the node never retries on its behalf, so that retry is
the DPP's to make.

`Idempotency-Key` is accepted on both paths and recorded in the adapter's audit trail, but
it **does not govern deduplication**. Two pushes with the same key and different `id`s are
two events; two pushes with the same `id` and different keys are one.

## What the adapter sees

On path B the DPP's `Authorization` header **never reaches the adapter**. The node strips
it, strips any inbound `X-CIRCULess-*`, and asserts its own (invariant 10):

| header | |
|---|---|
| `X-API-Key` | the credential from 1.3 |
| `X-CIRCULess-Subject` | stable pseudonymous id of the caller |
| `X-CIRCULess-Org` | the organisation they acted as |
| `X-CIRCULess-Resource` | the adapter's own registration id |
| `X-CIRCULess-Request-Id` | also in the node's access log |
| `X-CIRCULess-Actor` | the client the token was issued to — `svc-dpp` here |

Forwarded from the caller: `Content-Type`, `Accept`, `Content-Length`, `Idempotency-Key`.
Nothing else. There is no name and no email anywhere in this (D31).

**On path A none of that applies.** There is no node in front of that port to strip what a
caller sent, so the adapter believes nothing the request says about its identity: an event
arriving there is recorded with no actor, no subject and no organisation, however many
`X-CIRCULess-*` headers it carried.

## When it is refused

**Path A**, from the adapter:

| status | `reason` | |
|---|---|---|
| 401 | `invalid_token` | missing, malformed or wrong bearer. One answer for every variety, deliberately |
| 413 | `payload_too_large` | over the adapter's body limit |
| 422 | `invalid_request` | the body could not be read — see the contract above |

**Path B**, from the node before the adapter is reached:

| status | `reason` | |
|---|---|---|
| 401 | `invalid_token` | wrong `aud`, expired, more than one audience, or an unregistered key |
| 403 | `node_principal_not_permitted` | a node's token was used. Nodes consume nothing (D14) |
| 403 | `no_agreement` | no agreement permits this org — **or one exists for the wrong action** |
| 403 | `not_permitted` | visibility, not agreements: the resource is `org` or `private` and the caller is outside |
| 404 | `not_found` | wrong tenant or resource id; also what a caller sees for a resource they may not know exists |
| 405 | `unsupported` | the method is not in `idempotent_methods` |
| 413 | `payload_too_large` | over `max_request_bytes`, default 10 MiB |
| 502 / 504 | `upstream_error` / `upstream_timeout` | the adapter failed or exceeded `timeout_s` |

and then, from the adapter itself:

| status | `reason` | |
|---|---|---|
| 401 | `invalid_token` | the node's `X-API-Key` did not match. **A deployment fault, not the caller's** — step 1.3 |
| 403 | `actor_not_permitted` | the token is good and the agreement holds, but this actor is not in `DPP_ADAPTER_PUBLISHERS` — step 1.4 |
| 422 | `invalid_request` | as path A |

Note that `413` on path B comes from the **node**, not the adapter: the adapter's own body
limit guards the published port, which has no node in front of it to apply
`max_request_bytes`.

A correctly minted first token answering `403` is the system working, not a token problem.
`no_agreement` sends you to step 1.4's first half; `actor_not_permitted` to its second.
