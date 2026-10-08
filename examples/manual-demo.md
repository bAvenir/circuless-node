# Running the scenario by hand

A complete exchange between two organisations, on one machine, in about fifteen minutes.

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

The browser app is **not the node's admin UI** (N13). It is an example of what a partner
builds.

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

## 1. Two users

```sh
cd ../../circuless-cloud
export CIRCULESS_KEYCLOAK_ADMIN=<admin>
export CIRCULESS_KEYCLOAK_ADMIN_PASSWORD=<password>
KC=https://auth.circuless.bavenir.eu

deploy/keycloak/scripts/kc.py dev-user --url $KC --username alpha.admin --org alpha --admin
deploy/keycloak/scripts/kc.py dev-user --url $KC --username beta.admin  --org beta  --admin
```

Both get the password `Dev-Pa55word!` unless you pass `--user-password`.

**Two users, not one.** A person in both organisations would invoke Beta's service on
membership, and no agreement would be exercised at all.

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

## 10. The audit trail

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
| `401` from the service, passed through | the node's stored credential and `REFERENCE_SERVICE_KEY` differ. Re-run step 5 |
| `422 … internal socket` | `endpoint_url` resolves to loopback. Use the container name |
| the browser shows a network error, not a status | CORS. The node's allowed origins must include `http://127.0.0.1:5173` |
| sync errors in the node's log | expected; the node is not registered with the Cloud |
| `502 upstream_error: the upstream address could not be resolved` | the service container is not running. `docker compose -f docker-compose.demo.yml ps` — bring everything up with `up -d`, without naming a service |

## Tearing down

```sh
docker compose -f docker-compose.demo.yml down -v
```

`-v` removes the node's volume — its database, its uploaded bytes and its keypair.

## What has been verified, and by whom

Steps 1–8 and 10 were run end to end while this was written. **Step 9, in a real
browser, has not been** — browser automation is outside the node's test suite. What has
been checked mechanically is that the page's JavaScript parses and that its PKCE
challenge is byte-identical to the reference implementation's.

Everything behind the page — the decision, the credential, the proxy, the access log —
is covered by `tests/test_reference_service.py`.
