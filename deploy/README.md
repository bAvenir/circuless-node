# Installing a node on the CIRCULess overlay

For whoever runs the machine the node will live on. Perhaps twenty minutes, most of it
waiting for a peer to be approved.

You need, from the CIRCULess platform operator:

- a **NetBird setup key** (single-use) and the management server URL;
- the **node id** this node will be known by — it must match its Keycloak client;
- the realm URL and the Cloud API URL.

You need, on the machine: Docker with Compose, and outbound HTTPS. **No inbound ports,
and no public address.** That is the point of the overlay — a node behind a corporate
firewall or a home router works unchanged.

## Why two containers

The NetBird agent runs beside the node, and the node joins its network namespace. The
WireGuard interface therefore exists only inside that pair:

- the node **publishes no port on your machine** — check with `docker compose ps`;
- **nothing else on the machine is on the overlay**, so a mistake in the platform's
  access rules exposes one container rather than your estate;
- the node's loopback is shared with the agent and nothing else, which is how
  `/healthz` and `/metrics` stay off the overlay.

Running the agent on the host instead would put every listening service on the machine —
SSH, anything else you run — within reach of whichever peers the platform's rules allow.
That is not a trade we are willing to make on your behalf.

## 1. Configure

```sh
cd deploy
cp .env.example .env                       # node id, realm, Cloud API, CORS
cp secrets/netbird.env.example secrets/netbird.env
```

Put the setup key and management URL in `secrets/netbird.env`. Both files are gitignored.

Set `CIRCULESS_NODE_OPERATOR=partner` if this is **your** node holding **your** data. A
node left at the default `bvr` refuses anything classified `sensitive` (D22) — BVR does
not hold partners' sensitive data on their behalf.

## 2. Start

```sh
docker compose up -d
```

Three containers start: `migrate` applies the database schema and exits, then `netbird`
and `node` come up. The node generates its keypair on first start and prints the
certificate fingerprint.

A `migrate` service that has exited is the normal state — `docker compose ps -a` shows it
as `exited (0)`.

## 3. Approve the peer

The operator approves it in the NetBird console. Until then the agent is registered and
connected to nothing, which is intended: a leaked setup key gets you a peer that can
reach nothing.

## 4. Register the certificate

```sh
docker compose exec node circuless-node certificate
```

Send the output to the operator, who registers it on this node's Keycloak client. The
**private key never leaves the machine** — only the certificate does. There is nothing
here to put in a password manager.

## 5. Check

```sh
docker compose exec node circuless-node check
```

It confirms the node can obtain a Cloud token, and says so plainly if the certificate has
not been registered yet rather than leaving you reading Keycloak logs.

*(Overlay reachability joins this command in the next part of N15; until then, confirm
the peer is connected in the NetBird console.)*

## 6. Delete the setup key

```sh
rm secrets/netbird.env
```

It is single-use and was consumed at first start, so this is tidying rather than urgent —
but a spent credential in a file is still a credential in a file.

---

## What is where

| | |
|---|---|
| Database, resource content, keypair, certificate | the `node-data` volume, `/var/lib/circuless` |
| NetBird peer identity | the `netbird-state` volume |
| The private key's permissions | `0600`, owned by uid **10001** |

**The node refuses to start if anyone else can read its private key.** If you replace the
named volume with a bind mount, `chown 10001:10001` it first. Fixing the permissions
silently would hide that something loosened them, and on a shared host the key may
already have been copied.

## Upgrading

```sh
docker compose pull && docker compose up -d
```

Images are pinned by digest and signed with cosign, so this gets exactly the build that
was released and tested. Data and peer identity are in volumes and survive.

## Removing it

```sh
docker compose down          # stops; keeps the data and the peer identity
docker compose down -v       # removes both — the node re-enrols from scratch
```

Tell the operator either way, so they can remove the peer and the Keycloak client.

## Troubleshooting

**The node restarts repeatedly.** `docker compose logs node`. The usual causes are a
missing required setting — `CIRCULESS_NODE_NODE_ID` has no default — and
`CIRCULESS_NODE_CORS_ALLOW_ORIGINS` not being a JSON list. An empty value is not a list;
use `[]`.

**`check` says the certificate is not registered.** Step 4 has not been done, or was done
against a different client. The fingerprint `check` prints must match the one in Keycloak.

**Nothing can reach the node.** Confirm the peer is connected and approved in the NetBird
console, then confirm with the operator that an access rule allows their peer to reach
this one on TCP 8000. Default-deny means a peer with no rule reaches nothing — correct,
and indistinguishable from a broken install.

**The node cannot reach Keycloak.** It needs outbound HTTPS. The overlay is for inbound;
token verification and Cloud API calls go over the public internet.

## What the platform operator configures

The server side — groups, single-use keys, default-deny access rules, and keeping the
platform's own routes and DNS away from CIRCULess peers — is in the `circuless-cloud`
repository at `deploy/netbird/POLICIES.md`.
