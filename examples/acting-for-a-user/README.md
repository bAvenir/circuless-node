# Calling a node from a backend

Two of the three patterns in [`INTEGRATING-A-SERVICE.md`](../../INTEGRATING-A-SERVICE.md)
§11. The third — a browser calling the node directly — is [`../browser-app`](../browser-app).

|  | the node sees | decides against | use it for |
|---|---|---|---|
| **B** `as-itself` | your application | **your** organisation | work your application genuinely does on its own: a scheduled run, a background job |
| **C** `for-user` | the person, your application beside them | **their** organisation | acting on a request a person made, without a browser present |

**The difference is one grant type**, and it is the whole security story. Run `as-itself`
against data your organisation has no agreement for and the node refuses you, however
good your reasons. There is no privileged platform path, and a service that could read
anything would make every agreement on the platform advisory.

## Setup, once

```sh
uv run act.py keypair --client svc-workflows > certificate.pem
```

The private key stays in `svc.pem`, mode 0600, on your machine. Send
`certificate.pem` to the platform operator; they register it with
`kc.py register-cert`. Same arrangement as a node (D18, D26) — **no client secret is
ever issued**, and nothing you send is confidential.

## B — as itself

```sh
uv run act.py as-itself \
  --issuer   https://auth.circuless.bavenir.eu/realms/circuless \
  --client   svc-workflows --key svc.pem \
  --node     https://alpha-onprem.nodes.circuless.bavenir.eu \
  --audience node:alpha-onprem \
  --tenant   alpha --resource <uuid>
```

Client credentials, no exchange. The token carries `principal_type: service` and your
own `org_id`, and the node's access log will name your application.

## C — acting for a person

```sh
uv run act.py for-user ... --user-token "$TOKEN"
```

`$TOKEN` is the person's access token, and **its `aud` must already include this
service**. Keycloak refuses the exchange otherwise — "Client is not within the token
audience" — which is why their sign-in has to request the `svc:<service>` audience
scope. The operator configures that per application; it is a trust decision as much as
a technical one.

What comes back keeps the person as the subject and adds your application as `azp`:

```
  the person's token, before:
    sub             '44aa1814-…'      azp  'circuless-ui'
    aud             'svc-workflows'   principal_type  'user'    groups  ['/orgs/beta']
  the exchanged token, which the node will see:
    sub             '44aa1814-…'      azp  'svc-workflows'
    aud             'node:alpha-onprem'  principal_type  'user'  groups  ['/orgs/beta']
```

**Read the last two columns.** `principal_type` is still `user` and `groups` is still
the person's — the service's own type and organisation did not leak into the exchanged
token. That is D17, and the reason it matters is R1: a token claiming both the person's
organisation and the service's would leave "did the user act as BVR, or BVR as the
user?" to resolver order.

`--no-call` stops after the token and prints its claims. When an integration is not
working, that is the first question.

## This is not a production client

It holds a key on disk, takes a token on the command line and prints claims. Fine for
showing the flow and for running the acceptance cases; not how a service should treat
credentials.
