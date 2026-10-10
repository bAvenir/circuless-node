# Running the scenario by hand

A complete exchange between two organisations, on one machine, in about twenty-five
minutes — then the node's own admin UI over the top of it.

**Alpha owns the data. Beta owns the service. An Alpha user does the work.**

That arrangement is the point. Alpha reading Alpha's own file passes on membership;
Alpha invoking Beta's service needs an accepted agreement. One agreement, exercised
once, which is the smallest honest version of what the platform is for.

Everything runs against the **deployed Keycloak**, so the node and your browser see the
same issuer. A Keycloak reachable under two names mints tokens the node correctly
refuses, and the error — `invalid_token` on a token that looks perfectly valid — sends
people to the wrong place for an hour.

---

## What this is not

`docker-compose.demo.yml` is **not a deployment**. `../deploy/docker-compose.yml` is: it
runs the node beside a NetBird sidecar whose network namespace it shares, and publishes
no host port at all. This file publishes the node on loopback so a browser can reach it,
which is acceptable on a laptop and nowhere else.

**Two browser interfaces appear here, and they are not the same thing.** `browser-app/`
is an example of what a *partner* builds: their own application, their own identity, using
the node as a data plane. The node's **admin UI** (N13, §10) is the operator's own view of
one node, shipped inside the node's wheel and served from the node itself. The first is a
pattern to copy; the second is a part of the product.

## Why the node runs in a container

Because a node **refuses a loopback upstream**. Its own internal interface — `/metrics`,
`/internal/authz` — lives on `127.0.0.1`, and a provider who could register that address
would read them from outside. So `policy.check_upstream_url` refuses loopback at
registration and again on every call.

Inside the demo network the service is `reference-service:8080`, a private address,
which the guard permits — the ordinary case for a partner's service (D20). Running the
node directly on your host would leave nowhere to put the service.

## Prerequisites

Docker, Python 3, and Keycloak admin credentials for the deployed realm.

---

## 1. Three users

```sh
cd ../../circuless-cloud
export CIRCULESS_KEYCLOAK_ADMIN=<admin>
export CIRCULESS_KEYCLOAK_ADMIN_PASSWORD=<password>
KC=https://auth.circuless.bavenir.eu

deploy/keycloak/scripts/kc.py dev-user --url $KC --username alpha.admin --org alpha --admin
deploy/keycloak/scripts/kc.py dev-user --url $KC --username beta.admin  --org beta  --admin

# For §10 only: a member of Alpha who is not an admin of it.
deploy/keycloak/scripts/kc.py dev-user --url $KC --username alpha.user --org alpha
```

All get the password `Dev-Pa55word!` unless you pass `--user-password`.

**Separate users, not one.** A person in both organisations would invoke Beta's service on
membership, and no agreement would be exercised at all. `alpha.user` exists for a different
reason: being *in* an organisation and being able to *manage* it are different rights (N18),
and §10 is where that difference becomes visible.

### If you intend to do §10, do this now

The admin UI is served from the node's own origin, and the realm's `circuless-ui` client
does not yet allow it — sign-in will fail at the redirect with nothing useful to read.
`kc.py` **replaces** these lists rather than adding to them, so the browser app's values
have to be repeated or §9 breaks:

```sh
export CIRCULESS_UI_REDIRECT_URIS="http://localhost:5173/*,http://127.0.0.1:5173/*,http://127.0.0.1:8000/ui/*"
export CIRCULESS_UI_WEB_ORIGINS="http://localhost:5173,http://127.0.0.1:5173,http://127.0.0.1:8000"

deploy/keycloak/scripts/kc.py apply --url $KC
```

## 2. Start the node and the service

```sh
cd ../circuless-node/examples
export CIRCULESS_NODE_ISSUER=https://auth.circuless.bavenir.eu/realms/circuless
export REFERENCE_SERVICE_KEY=$(openssl rand -hex 24)

docker compose -f docker-compose.demo.yml up -d --build      # no service name
docker compose -f docker-compose.demo.yml ps                # both healthy
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/v1/whoami    # 401
```

`401` is the right answer: no route on a node is anonymous.

**The node's log will show sync failures.** It is not registered with the Cloud, and it
goes on serving regardless — that is F16, not a fault.

## 3. Create the tenants

A node has no API for this. Tenants are provisioned by an operator, never self-served.

```sh
docker compose -f docker-compose.demo.yml exec -T node python -c "
import uuid
from sqlmodel import Session, select
from circuless_node.db import create_db_engine
from circuless_node.models import Tenant
from circuless_node.settings import get_settings
engine = create_db_engine(get_settings())
with Session(engine) as s:
    for slug in ('alpha', 'beta'):
        if not s.exec(select(Tenant).where(Tenant.slug == slug)).first():
            s.add(Tenant(org_id=uuid.uuid4(), slug=slug, group_path=f'/orgs/{slug}'))
    s.commit()
    print('tenants:', [t.slug for t in s.exec(select(Tenant)).all()])
"
```

Expect `tenants: ['alpha', 'beta']`.

## 4. One token per user

```sh
cd ../../circuless-cloud
GT="deploy/keycloak/scripts/get-token.py --issuer $CIRCULESS_NODE_ISSUER --scope 'openid node:bvr-cloud'"

BETA=$(eval $GT)     # sign in as beta.admin
ALPHA=$(eval $GT)    # sign in as alpha.admin
```

Use a private window for the second, or sign out of Keycloak between them — otherwise
the session cookie signs you in as the first user again without asking.

`--scope 'openid node:bvr-cloud'` matters: the node accepts **only**
`aud = node:bvr-cloud`. A Cloud-audienced token gets a 401.

## 5. Beta registers its service

```sh
N=http://127.0.0.1:8000

SVC=$(curl -s -X POST $N/v1/t/beta/resources -H "Authorization: Bearer $BETA" \
  -H 'Content-Type: application/json' -d '{
    "slug":"csv-tools","kind":"service","title":"CSV tools","theme":"processing",
    "classification":"non-sensitive","licence":"CC-BY-4.0",
    "discoverability":"catalogue","visibility":"agreement",
    "endpoint_url":"http://reference-service:8080"
  }' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "service: $SVC"

curl -s -X PUT $N/v1/t/beta/resources/$SVC/credential -H "Authorization: Bearer $BETA" \
  -H 'Content-Type: application/json' \
  -d "{\"scheme\":\"header\",\"header_name\":\"X-API-Key\",\"secret\":\"$REFERENCE_SERVICE_KEY\"}"
```

An organisation's **admins** set a credential, never an automated account, and no
interface returns the value afterwards.

`discoverability: catalogue` is what would publish this service so another organisation
could *find* it — the Cloud catalogue carries the resource id and the node that hosts
it, which is how a consumer learns both. It will not actually be published here: the
push needs this node registered with the Cloud, and it is not. In this walkthrough Beta
simply tells Alpha the id, which is also how a bilateral agreement usually starts.

The default is `hidden` (NFR4 — defaults are closed), so omitting this field leaves a
resource that nobody can discover.

## 6. Alpha registers and uploads its data

```sh
printf 'sample_id,polymer,mass_g\n1,PET,4.2\n2,HDPE,3.1\n' > /tmp/batch-7.csv

CSV=$(curl -s -X POST $N/v1/t/alpha/resources -H "Authorization: Bearer $ALPHA" \
  -H 'Content-Type: application/json' -d '{
    "slug":"batch-7","kind":"dataset","title":"Recycled PET batch 7",
    "theme":"material-characterisation","classification":"non-sensitive",
    "licence":"CC-BY-4.0"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "dataset: $CSV"

curl -s -X PUT $N/v1/t/alpha/resources/$CSV/data -H "Authorization: Bearer $ALPHA" \
  -H 'Content-Type: text/csv' --data-binary @/tmp/batch-7.csv
```

## 7. See it refused, before the agreement exists

Worth doing in this order — it shows the refusal happens at the node and the partner's
service is never contacted.

```sh
curl -s $N/v1/t/alpha/resources/$CSV/data -H "Authorization: Bearer $ALPHA"
# the CSV: Alpha reads its own file on membership

curl -s -X POST $N/v1/t/beta/resources/$SVC/invoke/headers \
  -H "Authorization: Bearer $ALPHA" -H 'Content-Type: text/csv' \
  --data-binary @/tmp/batch-7.csv
# {"reason":"no_agreement", ...}
```

## 8. The agreement

In a complete platform this arrives from the Cloud through sync. This node is not
registered, so cache it directly:

```sh
cd ../circuless-node/examples
docker compose -f docker-compose.demo.yml exec -T node python -c "
import uuid, datetime as dt
from sqlmodel import Session
from circuless_node.db import create_db_engine
from circuless_node.models import AgreementCache
from circuless_node.settings import get_settings
with Session(create_db_engine(get_settings())) as s:
    s.add(AgreementCache(id=uuid.uuid4(), provider_org='beta', consumer_org='alpha',
        resource_id=uuid.UUID('$SVC'), actions='invoke',
        valid_from=dt.datetime.now(dt.UTC) - dt.timedelta(days=1), status='accepted'))
    s.commit(); print('agreement cached')
"
```

Run the invoke from step 7 again:

```json
{"columns": ["sample_id", "polymer", "mass_g"], "count": 3}
```

## 9. From the browser

```sh
cd browser-app && python3 -m http.server 5173
```

Open `http://127.0.0.1:5173/` and fill in:

| | |
|---|---|
| Identity provider | `https://auth.circuless.bavenir.eu/realms/circuless` |
| Node base URL | `http://127.0.0.1:8000` |
| Node audience | `node:bvr-cloud` |
| Data tenant / Service tenant | `alpha` / `beta` |
| Dataset resource id | from step 6, or **List resources I administer** |
| Service resource id | from step 5 — paste it; see below |

Sign in **as alpha.admin**, then *Download, then extract headers*.

**Listing Beta's resources returns 403, and that is correct.** Listing is a management
call — the owner's view of their own registry — so only an administrator of that
organisation may ask. An agreement lets Alpha *use* Beta's service; it never lets Alpha
enumerate what else Beta has. A consumer finds a service through the catalogue, which
shows what its owner chose to publish. Paste the service id from step 5.

Watch the token panel. It shows one access token per audience, minted from a single
refresh token — and, in red, the `aud` of the token it threw away from sign-in: the one
carrying every audience requested, which every API rejects.

## 10. The node's own admin UI

Everything so far was `curl` and a partner's application. This is the operator's view —
N13, served by the node itself at `/ui`, from files inside its wheel. Nothing is fetched
from the internet to render it, which is the requirement for a node on a partner's
premises.

Open **http://127.0.0.1:8000/ui/** and sign in as **alpha.admin**.

No token to paste and no fields to fill in: the page reads `config.json`, which the node
writes at startup from its own settings, and does the PKCE exchange itself.

**`/ui` is the one anonymous path on a node** — the single exemption to D21, because a
browser cannot present a token for the request that fetches the code which obtains the
token. Nothing under it carries tenant data or node state; `config.json` holds four keys,
and a fifth fails a test and the node's own startup check.

### What to look at

Work down this list; each item is behaviour that no test in the suite can reach.

**Signing in.** After the redirect, the address bar should hold no `?code=`. Reload: you
stay signed in rather than bouncing back to the sign-in screen.

**Membership against management.** The tenant list shows `alpha` with a green
*Can manage* pill, and the slug is a link. Now open a private window and sign in as
**alpha.user**: same tenant, *Member, cannot manage*, and the slug is deliberately **not**
a link. Listing resources is a management call, so the link would lead to a 403 — saying
so is better than handing someone a dead click. This distinction is the reason
`GET /v1/tenants` returns `can_manage` at all.

Back in the admin's window:

**The resources you already made.** `batch-7` from §6 is there. Open it: a dataset's
detail view, with its storage path and its two-stage deletion state (empty, for now).

Beta's service is the more interesting one, and you will see it as **beta.admin** further
down: a service's detail view carries `endpoint_url`, which is shown to whoever may manage
the resource and to nobody else. It never reaches the catalogue — a consumer who knew the
upstream address could go round the node, past `decide()`, past the agreement check and
past the log.

**Registering.** *Register a resource* → switch Kind between `dataset` and `service` a
couple of times. The fields swap — `storage_path` for a dataset, `endpoint_url` and
`invoke_policy` for a service — and **what you have already typed survives the swap**.
The form offers only the fields the node accepts for that kind; it does not re-implement
the node's rules about *values*.

Look at Classification: `sensitive` is greyed out, reading *this node does not hold
sensitive data*. That comes from `capabilities.accepts_sensitive` on
`/.well-known/circuless-node`, not from anything baked into the page — a BVR-operated node
refuses it (D22) and an on-premises one would not.

**A refusal that must not lose your work.** Set Discoverability to `catalogue` and leave
Licence as *None*. The node answers `licence_required` (NFR9), and the message appears
**above the form with every field still filled in**. A long form that empties itself on a
422 is the thing this avoids.

Give it a licence and register it as a `file` dataset called `ui-scratch`.

**Uploading.** On `ui-scratch`, choose a file big enough to watch — a hundred megabytes or
more — and upload it. The progress bar should move, then the line reads
`Stored N bytes at …`. Upload is the one call that does not use `fetch`: it needs XHR to
report progress at all, and the node's default limit is a gibibyte.

**Withdrawing.** *Withdraw* on `ui-scratch` — not on `batch-7`, which later sections use.

Read the confirmation before clicking through it. It says consumers lose access
immediately and the data is kept until the purge date; it does **not** say "delete",
because the bytes are still there and will be until the purge job runs (N20, D25). The
button stays disabled until you type `ui-scratch` exactly — the point is to make someone
read *which* resource this is while a list of similar names is one click away.

Afterwards the detail page carries the withdrawn notice and its purge date, and both the
upload panel and the Withdraw button are gone.

**The credential.** Open Beta's service in a second private window as **beta.admin** and
go to *Credential*. It reports a `header` credential set on `X-API-Key` from §5 and
**never shows the value** — no API and no interface returns it (invariant 11).

> **§11 depends on this credential.** Submitting the form replaces it and *Remove* deletes
> it, and either way the invoke in §11 comes back as `bad or missing X-API-Key` — the
> service's own words, passed through, which reads like a node fault and is not one.
> Follow the next paragraph exactly, or look without touching.

Change the scheme and watch the username and header-name rows appear and disappear:
`basic` needs a username, `header` needs a header name, `bearer` needs neither. Then set
it back to `header`, put `X-API-Key` in the header name, and paste the **same** key into
Secret:

```sh
echo $REFERENCE_SERVICE_KEY        # in the shell from §2
```

Submit. That exercises the rotate path properly and leaves §11 working, because the value
you wrote is the one the service already expects. The page should then report the
credential as updated just now.

If you have already lost it, this puts it back — reading the key from the container, so
the two cannot disagree:

```sh
KEY=$(docker compose -f docker-compose.demo.yml exec -T reference-service printenv REFERENCE_SERVICE_KEY | tr -d '\r\n')
curl -s -X PUT $N/v1/t/beta/resources/$SVC/credential -H "Authorization: Bearer $BETA" \
  -H 'Content-Type: application/json' \
  -d "{\"scheme\":\"header\",\"header_name\":\"X-API-Key\",\"secret\":\"$KEY\"}"
```

**The access log.** Back as alpha.admin, *Access log*. Everything above is in it,
including the registration you had refused. Filter to *Denied* and find the
`licence_required` attempt. Then press the browser's back button: it returns to the
unfiltered view, because the filters live in the URL fragment rather than in a variable —
for a page whose job is answering "what happened", a link someone can paste is most of
its value.

The page also says that reading the log is itself a management decision and appears in
the log. Reload and you will see your own read.

## 11. The audit trail

```sh
curl -s $N/v1/t/alpha/access-log -H "Authorization: Bearer $ALPHA" | python3 -m json.tool
curl -s $N/v1/t/beta/access-log  -H "Authorization: Bearer $BETA"  | python3 -m json.tool
```

**Two logs, two tenants, one person.** Alpha's holds the read; Beta's holds the invoke
with `acting_org: alpha`. Neither holds a name or an email — node tokens carry neither,
so there is nothing to record.

Try the error path too, and watch it reach you unchanged:

```sh
curl -s -X POST $N/v1/t/beta/resources/$SVC/invoke/headers \
  -H "Authorization: Bearer $ALPHA" -H 'Content-Type: text/csv' --data-binary ''
# {"error":"the body is empty; send a CSV"} — the service's own words, 422
```

---

## Switching the service to tier 1

`service.py` reads no CIRCULess header at all. To see the identity-aware version:

```sh
SERVICE_MODULE=identity docker compose -f docker-compose.demo.yml up -d
curl -s -X GET $N/v1/t/beta/resources/$SVC/invoke/whoami -H "Authorization: Bearer $ALPHA"
```

That returns exactly what the node told the service — and the full list of headers that
survived the allowlist, which is the quickest way to see the contract.

Note that the tier-0 run above worked with **no identity code in the service at all**.
That is the claim this example exists to demonstrate.

## When something does not work

| symptom | cause |
|---|---|
| `401 invalid_token` on a token that looks fine | wrong audience. The node wants `aud = node:bvr-cloud` exactly; a Cloud token or a multi-audience token is refused |
| `403 not_permitted` invoking | you are signed in as the wrong user. Alpha consumes; Beta owns |
| `403 not_permitted … not a member of the organisation that owns this tenant` when **listing** | expected. Listing is an owner's call; an agreement does not grant it. Paste the resource id instead |
| `403 no_agreement` after step 8 | the agreement names a different `resource_id`. Check `$SVC` |
| `401` / `bad or missing X-API-Key` from the service, passed through | the node's stored credential and `REFERENCE_SERVICE_KEY` differ, or there is no credential at all. Three ways to get here: a new shell regenerated the key, `up -d --build` recreated the service with a new one, or §10's credential page was submitted or *Remove*d. Check with `GET …/credential` — a 404 means it was deleted — then re-run step 5 |
| the invoke worked before §10 and not after | §10 touches Beta's credential. See the restore command at the end of §10 |
| `422 … internal socket` | `endpoint_url` resolves to loopback. Use the container name |
| the browser shows a network error, not a status | CORS. The node's allowed origins must include `http://127.0.0.1:5173` |
| sync errors in the node's log | expected; the node is not registered with the Cloud |
| §10: *The identity provider rejected the exchange* | `http://127.0.0.1:8000/ui/*` is not a redirect URI on `circuless-ui`. The step at the end of §1 was skipped, or a later `kc.py apply` ran without those variables set and replaced the list |
| §10: *This node is not serving its UI configuration* | `config.json` is missing. It is written at startup into the data directory, so this means that directory is not writable |
| §10: the admin UI loads but every call fails | not CORS — the UI is served from the same origin as the API, so the allowed-origins list is not involved. Check the audience: the UI asks for `openid node:bvr-cloud`, which must match this node's id |
| §10: signed in, but the tenant list is empty | the user is in no organisation this node hosts. Step 3 creates the tenants; step 1 puts the user in `/orgs/alpha` |
| §10: a blank area where something should be | a markup or script fault the structural tests did not catch. Worth reporting with the view name |
| `502 upstream_error: the upstream address could not be resolved` | the service container is not running. `docker compose -f docker-compose.demo.yml ps` — bring everything up with `up -d`, without naming a service |

## Tearing down

```sh
docker compose -f docker-compose.demo.yml down -v
```

`-v` removes the node's volume — its database, its uploaded bytes and its keypair.

## What has been verified, and by whom

Steps 1–8 and 11 were run end to end while this was written. **Steps 9 and 10, in a
real browser, have not been** — browser automation is outside the node's test suite, and
adding a runner for it would mean the build step CLAUDE.md rules out.

What *is* checked mechanically for §10 is narrower than it looks, and worth knowing
before you trust a green suite:

| checked in CI | not checked anywhere but here |
|---|---|
| every API path the UI calls is a route the node serves | that any of it renders |
| every field it reads is a key the node returns | the sign-in round trip and PKCE against a real Keycloak |
| every element id it looks up exists in the markup | that the progress bar moves |
| the vocabularies match the node's enums | that a refusal leaves the form filled in |
| no `innerHTML`, no inline handlers, no literal colours | the back button across fragment routes |
| nothing is fetched from outside the node or its issuer | anything about how it looks |

So a green suite means the UI is wired to the right API and cannot silently render
markup; it says nothing about whether a person can use it. That is what §10 is for.

Everything behind both pages — the decision, the credential, the proxy, the access log —
is covered by `tests/test_reference_service.py`.
