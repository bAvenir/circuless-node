# `svc-dpp` → `POST /v1/events`

A worked path for one specific integration: the **DPP** pushes an event into the
**DPP_Adapter** service, which runs behind the node and is reached only through it.

Everything here is an instance of [`INTEGRATING-A-SERVICE.md`](../../INTEGRATING-A-SERVICE.md)
— §8 for the registration, §11 pattern B for the token. Nothing below is a special case;
if it reads like one, something has been set up wrong.

## The one thing to understand first

**The DPP does not call `/v1/events`.** There is no such route on a node. It calls:

```
POST https://$NODE_HOST/v1/t/$TENANT/resources/$RESOURCE/invoke/events
```

and the node forwards it to `$ENDPOINT_URL/events`. With the adapter registered at
`http://dpp-adapter:8000/v1`, that lands on the adapter's own `POST /v1/events` — which
is the path the adapter's OpenAPI document declares and the only one it needs to know
about. The node's prefix is the node's business.

Every one of those calls is an authorization decision and an access-log entry. That is
the whole reason for routing the DPP through a node rather than giving it the adapter's
address.

## Settings used throughout

```sh
NODE_ID=bvr-node                                    # see the note below
NODE_HOST=$NODE_ID.nodes.circuless.bavenir.eu
KEYCLOAK=https://auth.circuless.bavenir.eu                 # kc.py takes this
ISSUER=$KEYCLOAK/realms/circuless                          # the DPP takes this
TENANT=bvr                                          # the tenant that owns the adapter
```

> `deploy/keycloak/realm-config.json` in `circuless-cloud` currently declares four nodes —
> `bvr-cloud`, `alpha-onprem`, `jozef-node`, `ltu-node`. If the node hosting the adapter is
> one of those rather than a newly enrolled `bvr-node`, substitute its id everywhere; the
> audience string and the hostname both derive from it.

---

# Part 1 — the platform operator, once

## 1.1 Declare the client

Add to `clients` in `circuless-cloud/deploy/keycloak/realm-config.json`, modelled on
`svc-workflows`:

```json
{
  "clientId": "svc-dpp",
  "name": "DPP event publisher",
  "description": "The DPP, as a service principal. Pushes events to the DPP_Adapter behind a node; pattern B only.",
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

Two fields decide the security of this integration, and both are judgement calls:

| field | what turns on it |
|---|---|
| `org_id` | the organisation the node decides *against*. Set it to the partner's own org and the DPP needs an agreement, and the access log names them as a third party. Set it to `bvr` and there is no agreement to revoke. |
| `audiences` | `all` lets `svc-dpp` mint a token for any node. Narrowing it to the one node the adapter sits on is the stronger setting and costs nothing here, since the DPP calls exactly one node. |

Note what is **absent**: `standard.token.exchange.enabled`. The DPP acts only as itself —
it has no person to act for — and withholding exchange means it can never acquire one.

Then:

```sh
CIRCULESS_KEYCLOAK_ADMIN_PASSWORD=... \
  python3 deploy/keycloak/scripts/kc.py apply --url $KEYCLOAK
```

## 1.2 Register the partner's certificate

The DPP sends a certificate (step 3.1). It is not confidential; any channel is fine.

```sh
CIRCULESS_KEYCLOAK_ADMIN_PASSWORD=... \
  python3 deploy/keycloak/scripts/kc.py register-cert \
    --client svc-dpp --cert dpp.pem.crt --url $KEYCLOAK
```

**No client secret is issued, in either direction.** That is `private_key_jwt` (D18) — the
same arrangement every node and every service here uses.

---

# Part 2 — the organisation running the adapter

## 2.1 Run the adapter beside the node

[`../add-your-service`](../add-your-service) is the pattern. The adapter gets **no host
port**: nothing but the node can reach it, which is why it may trust the `X-CIRCULess-*`
headers at all.

## 2.2 Register it as a service resource

```sh
curl -sX POST https://$NODE_HOST/v1/t/$TENANT/resources \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"slug":"dpp-adapter","kind":"service","title":"DPP Adapter",
       "theme":"processing","classification":"non-sensitive",
       "endpoint_url":"http://dpp-adapter:8000/v1",
       "invoke_policy":{"idempotent_methods":["GET","POST"],"timeout_s":30}}'
```

Keep the `id` from the response — that is `$RESOURCE`, and the DPP needs it.

- `endpoint_url` carries the `/v1`, so the adapter's own paths stay as it publishes them.
- `idempotent_methods` is an **allowlist of methods, not a retry list** — the node never
  retries. `GET, POST` is all this adapter needs; leaving `PUT` and `DELETE` out means a
  caller cannot reach them even by guessing a path.
- Loopback, `0.0.0.0` and link-local are refused at registration and again on every call.
  A container name on the node's private network is the intended shape.

## 2.3 Give the node the adapter's key

If the adapter checks a shared key — it should, as defence in depth behind the network
boundary:

```sh
curl -sX PUT https://$NODE_HOST/v1/t/$TENANT/resources/$RESOURCE/credential \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"scheme":"header","header_name":"X-API-Key","secret":"<the adapter's key>"}'
```

Stored encrypted, returned by no interface afterwards (invariant 11). To change it, set a
new one.

## 2.4 Permit the DPP

With `org_id: dpp`, an agreement is required: in the console, from `bvr` to `dpp`, on this
resource, action **`invoke`**. An agreement granting `read` instead refuses the call with
`no_agreement` — which reads like no agreement at all rather than like the wrong action.

With `org_id: bvr` and the resource at visibility `org`, membership alone permits it and
no agreement exists. Convenient, and worth being deliberate about: there is then nothing
to revoke.

---

# Part 3 — the DPP partner

This is the part to hand over. It needs three values from you: `$ISSUER`, `$NODE_HOST`,
and the `$RESOURCE` uuid.

## 3.1 Generate a keypair, once

```sh
uv run act.py keypair --client svc-dpp --key dpp.pem > dpp.pem.crt
```

([`../acting-for-a-user/act.py`](../acting-for-a-user/act.py))

The private key is written `0600` and **never leaves the machine**. Send `dpp.pem.crt` to
the platform operator. Nothing sent is secret.

## 3.2 Mint a token, per call

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

- **One audience per token.** A token carrying two is replayable between the two systems
  that accept it, and every API here rejects it. `scope` names exactly one node.
- **The assertion's `aud` is the token endpoint, not the node.** Getting this wrong
  produces `invalid_client`, which reads like an unregistered certificate and sends you
  looking in the wrong place.
- **Access tokens last five minutes.** Mint per call, or cache with a margin.

## 3.3 Push an event

```sh
curl -sX POST https://$NODE_HOST/v1/t/$TENANT/resources/$RESOURCE/invoke/events \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"type":"dpp.updated","id":"...","occurred_at":"2026-11-15T10:00:00Z","payload":{}}'
```

`Idempotency-Key` is one of the four headers forwarded verbatim, so the adapter can
deduplicate a retry. **The node never retries** — a dropped connection is a failure
reported to you — so the retry is yours to make, and the key is what keeps it safe.

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

## What the adapter sees

The DPP's `Authorization` header **never reaches the adapter**. The node strips it, strips
any inbound `X-CIRCULess-*`, and asserts its own (invariant 10):

| header | |
|---|---|
| `X-API-Key` | the credential from 2.3, in whatever scheme was registered |
| `X-CIRCULess-Subject` | stable pseudonymous id of the caller |
| `X-CIRCULess-Org` | the organisation they acted as |
| `X-CIRCULess-Resource` | the adapter's own registration id |
| `X-CIRCULess-Request-Id` | also in the node's access log |
| `X-CIRCULess-Actor` | the client the token was issued to — `svc-dpp` here |

Forwarded from the caller: `Content-Type`, `Accept`, `Content-Length`, `Idempotency-Key`.
Nothing else. There is no name and no email anywhere in this (D31).

**The adapter must police push against read itself.** The node's decision is per resource
and per action, not per path or per method: a caller the agreement permits to `invoke` may
call *any* path on the adapter with any allowed verb. So if the same registration both
accepts events and serves them to Workflows, it is the adapter that must decide `svc-dpp`
may `POST events` and not `GET feed`, from `X-CIRCULess-Actor` and `X-CIRCULess-Org`.
Two separate registrations, one per direction, move that line back onto the agreement —
which is where it belongs if the two sides are different organisations.

## When it is refused

| status | body `reason` | what it is |
|---|---|---|
| 401 | `invalid_token` | expired, signed by an unregistered key, or an `aud` that is not exactly `node:$NODE_ID` — including one that carries a second audience |
| 403 | `node_principal_not_permitted` | a node's token was used. Nodes consume nothing (D14) |
| 403 | `no_agreement` | no agreement permits this org — **or one exists for the wrong action** |
| 403 | `not_permitted` | visibility, not agreements: the resource is `org` or `private` and the caller is outside |
| 404 | `not_found` | wrong tenant or resource id; also what a caller sees for a resource they may not know exists |
| 405 | `unsupported` | the method is not in `idempotent_methods` |
| 413 | `payload_too_large` | over `max_request_bytes`, default 10 MiB |
| 502/504 | `upstream_error` / `upstream_timeout` | the adapter failed or exceeded `timeout_s` |

A correctly minted first token answering `403 no_agreement` is the system working, not a
token problem. Check 2.4 before re-reading part 3.
