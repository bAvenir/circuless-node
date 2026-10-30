# Add your service

Run your service beside the node, so the node can reach it and nothing else can.

`docker-compose.override.yml` adds one service to the node's compose. The example is
`minimal.py` from [`reference-service`](../reference-service). It checks `X-API-Key`,
answers `GET /healthz`, and returns the column names of a CSV you `POST`.

## 1. Start it

```sh
cp examples/add-your-service/docker-compose.override.yml deploy/
echo "MY_SERVICE_KEY=$(openssl rand -hex 24)" >> deploy/.env
cd deploy && docker compose up -d
```

Compose loads the override on its own. The service gets no host port.

## 2. Register it on the node

`endpoint_url` is the service name from the override.

```sh
curl -s -X POST $N/v1/t/$ORG/resources \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"slug":"my-service","kind":"service","title":"My service",
       "theme":"processing","classification":"non-sensitive",
       "endpoint_url":"http://my-service:8080",
       "invoke_policy":{"idempotent_methods":["GET","POST"]}}'
```

`N` is your node's public URL, `ORG` your organisation, `TOKEN` a token for your node.
Keep the `id` from the response as `SVC`.

## 3. Give the node the key, if your service checks one

The example does. The node stores the key encrypted and sends it as `X-API-Key` on every call.

```sh
curl -s -X PUT $N/v1/t/$ORG/resources/$SVC/credential \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"scheme":"header","header_name":"X-API-Key","secret":"<MY_SERVICE_KEY from deploy/.env>"}'
```

## 4. Call it

```sh
curl -s $N/v1/t/$ORG/resources/$SVC/invoke/healthz -H "Authorization: Bearer $TOKEN"

curl -s -X POST $N/v1/t/$ORG/resources/$SVC/invoke/columns \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: text/csv' --data-binary $'a,b,c\n1,2,3'
```

The second returns `{"columns": ["a", "b", "c"], "count": 3}`.

## Your own service

Edit `my-service` in the override:

- replace `build` and `command` with your service, or use `image:`;
- set its port in `endpoint_url`;
- if it checks a key, pass it in `environment` and give the node the same value in step 3.

Do not add `ports:`. Your service then trusts the `X-CIRCULess-*` headers only because
nothing but the node can reach it ([`INTEGRATING-A-SERVICE.md`](../../INTEGRATING-A-SERVICE.md) §3).
