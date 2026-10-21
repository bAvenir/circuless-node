# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography>=44", "pyjwt[crypto]>=2.10"]
# ///
"""Calling a node from a backend — as yourself, or on behalf of a person.

    uv run act.py keypair   --client svc-workflows
    uv run act.py as-itself --client svc-workflows --key svc.pem ...
    uv run act.py for-user  --client svc-workflows --key svc.pem --user-token ... ...

`INTEGRATING-A-SERVICE.md` §11 describes three patterns for reaching a node, and they
differ in **whose identity the node sees** — which decides what the access log can tell
you afterwards. `examples/browser-app` demonstrates pattern A, where the browser calls
the node and the log names the person. This demonstrates the other two:

**B — as itself.** Your backend authenticates as its own service account. The node sees
your application, decides against *your* organisation, and logs your application. Right
for a scheduled run; wrong for anything a person asked for.

**C — acting for a person.** Your backend exchanges the person's token for one the node
accepts. The node sees the person, decides against *their* organisation, and logs your
application alongside them as the actor. Right for a backend acting on a request.

The difference between the two is one grant type, and it is the whole of the security
story: run `as-itself` against a resource your organisation has no agreement for and the
node refuses you, however legitimate your reasons. There is no privileged platform path.

## What it is not

A production client. It holds a key on disk, takes a user token on the command line and
prints claims — all fine for showing how the flow works and for running the acceptance
cases, none of it how a service should treat credentials.

Nothing here is secret. The private key you generate is yours and never leaves the
machine; only the certificate goes to the platform operator, exactly as for a node.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"  # nosec B105
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # nosec B105
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"  # nosec B105


# --- enrolment ----------------------------------------------------------------------------


def keypair(args: argparse.Namespace) -> int:
    """Generate a keypair and print the certificate to register.

    The same shape a node uses (D18, D26): the private half never leaves this machine,
    and the operator registers the public half on your client. There is no client
    secret anywhere in this flow — that is the point of `private_key_jwt`.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = pathlib.Path(args.key)
    if path.exists() and not args.force:
        raise SystemExit(
            f"{path} exists. Use --force only if you mean to replace it — the\n"
            "  certificate registered for this client will stop matching."
        )

    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, args.client)])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=args.days))
        .sign(key, hashes.SHA256())
    )

    print(f"private key written to {path} (0600) — it does not leave this machine", file=sys.stderr)
    print("send the operator everything below; they run `kc.py register-cert`\n", file=sys.stderr)
    print(certificate.public_bytes(serialization.Encoding.PEM).decode(), end="")
    return 0


# --- getting a token ------------------------------------------------------------------------


def token_endpoint(issuer: str) -> str:
    return f"{issuer.rstrip('/')}/protocol/openid-connect/token"


def client_assertion(issuer: str, client: str, key_path: str) -> str:
    """`private_key_jwt`: the client proves itself by signing, not by presenting a secret."""
    pem = pathlib.Path(key_path).read_bytes()
    now = dt.datetime.now(dt.UTC)
    return jwt.encode(
        {
            "iss": client,
            "sub": client,
            "aud": token_endpoint(issuer),
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + dt.timedelta(minutes=1),
        },
        pem,
        algorithm="RS256",
    )


def post_form(url: str, fields: dict[str, str]) -> tuple[int, dict]:
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(  # nosec B310 — https, from the issuer argument
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
            return response.status, json.load(response)
    except urllib.error.HTTPError as refused:
        return refused.code, json.loads(refused.read() or b"{}")


def claims_of(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def show(label: str, token: str) -> None:
    claims = claims_of(token)
    print(f"  {label}")
    for field in ("sub", "azp", "aud", "principal_type", "org_id", "groups", "act"):
        if field in claims:
            print(f"    {field:15} {claims[field]!r}")


# --- the two patterns -------------------------------------------------------------------------


def as_itself(args: argparse.Namespace) -> int:
    """Pattern B. Client credentials: the node sees your application and nobody else."""
    status, payload = post_form(
        token_endpoint(args.issuer),
        {
            "grant_type": "client_credentials",
            "client_id": args.client,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": client_assertion(args.issuer, args.client, args.key),
            "scope": args.audience,
        },
    )
    if status != 200:
        raise SystemExit(f"the identity provider refused ({status}): {payload}")

    show("the token this backend will present:", payload["access_token"])
    return call_node(args, payload["access_token"])


def for_user(args: argparse.Namespace) -> int:
    """Pattern C. Token exchange: the node sees the person, and your application beside them.

    The subject token must already carry this service in its audience — Keycloak refuses
    the exchange otherwise, with "Client is not within the token audience". That is what
    the `svc:<service>` audience scope is for, and why a person's client has to request
    it at sign-in.
    """
    status, payload = post_form(
        token_endpoint(args.issuer),
        {
            "grant_type": EXCHANGE_GRANT,
            "client_id": args.client,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": client_assertion(args.issuer, args.client, args.key),
            "subject_token": args.user_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "scope": args.audience,
        },
    )
    if status != 200:
        raise SystemExit(
            f"the exchange was refused ({status}): {payload}\n"
            "  'Client is not within the token audience' means the person's token does not\n"
            f"  carry {args.client} in its `aud`. They must sign in asking for the\n"
            f"  svc:… audience scope as well as the node's."
        )

    show("the person's token, before:", args.user_token)
    show("the exchanged token, which the node will see:", payload["access_token"])
    return call_node(args, payload["access_token"])


def node_url(args: argparse.Namespace) -> str:
    """Where on the node this call goes.

    Two shapes, because a resource is one of two things. A **dataset** has bytes, which
    you read; a **service** has an endpoint, which you invoke and the node proxies. The
    agreement has to permit the matching action — `read` or `invoke` — and an agreement
    for the wrong one refuses with `no_agreement`, which reads like no agreement at all.
    """
    base = f"{args.node.rstrip('/')}/v1/t/{args.tenant}/resources/{args.resource}"
    if args.invoke is None:
        return f"{base}/data"
    return f"{base}/invoke/{args.invoke.lstrip('/')}"


def call_node(args: argparse.Namespace, token: str) -> int:
    """Call the node with the token just obtained, and report what it said."""
    if args.no_call:
        # "Did I get the right token?" is the first question when an integration does
        # not work, and it is answerable without a node being involved at all.
        print("\n  --no-call: stopping before the node")
        return 0

    url = node_url(args)
    body = None
    headers = {"Authorization": f"Bearer {token}"}
    if args.body is not None:
        # `@name` reads a file, anything else is the literal bytes — the convention
        # curl uses, so nobody has to learn a second one.
        body = (
            pathlib.Path(args.body[1:]).read_bytes()
            if args.body.startswith("@")
            else args.body.encode()
        )
        headers["Content-Type"] = args.content_type

    # `Authorization` is the only credential sent, and the node does **not** pass it on:
    # it strips it, along with any inbound `X-CIRCULess-*`, and injects its own headers
    # naming the subject, the acting organisation and the resource (invariant 10). So a
    # service behind the node never sees this token and cannot be made to trust one.
    request = urllib.request.Request(  # nosec B310 — https, from the --node argument
        url, data=body, headers=headers, method=args.method
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:  # nosec B310
            answered = response.read()
        print(f"\n  {args.method} {url}\n  {response.status} — {len(answered)} bytes")
        if answered[:400].strip():
            print(f"  {answered[:400].decode(errors='replace')}")
        return 0
    except urllib.error.HTTPError as refused:
        detail = (refused.read() or b"").decode()[:300]
        print(f"\n  {args.method} {url}\n  {refused.code} — {detail}")
        # A refusal is the expected answer for pattern B against someone else's data,
        # so this is not an error exit: the caller decides what they were testing.
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    enrol = sub.add_parser("keypair", help="generate a keypair; print the certificate to register")
    enrol.add_argument("--client", required=True, help="the client id, e.g. svc-workflows")
    enrol.add_argument("--key", default="svc.pem", help="where to write the private key")
    enrol.add_argument("--days", type=int, default=365)
    enrol.add_argument("--force", action="store_true", help="replace an existing key")
    enrol.set_defaults(run=keypair)

    for name, run, needs_user in (("as-itself", as_itself, False), ("for-user", for_user, True)):
        call = sub.add_parser(name, help=run.__doc__.splitlines()[0])
        call.add_argument("--issuer", required=True, help="https://<host>/realms/circuless")
        call.add_argument("--client", required=True)
        call.add_argument("--key", required=True, help="the private key from `keypair`")
        call.add_argument("--node", required=True, help="https://<node>.nodes.<domain>")
        call.add_argument("--audience", required=True, help="node:<node_id>")
        call.add_argument("--tenant", required=True)
        call.add_argument("--resource", required=True, help="the resource uuid")
        call.add_argument(
            "--invoke",
            metavar="PATH",
            help="call the resource's service at this path instead of reading its data; "
            "the agreement must permit `invoke` rather than `read`",
        )
        call.add_argument("--method", default="GET", help="for --invoke; default GET")
        call.add_argument("--body", help="for --invoke; a literal string, or @file")
        call.add_argument("--content-type", default="application/json")
        call.add_argument("--timeout", type=float, default=30.0)
        call.add_argument(
            "--no-call",
            action="store_true",
            help="stop after obtaining the token, and show its claims",
        )
        if needs_user:
            call.add_argument(
                "--user-token",
                required=True,
                help="the person's access token; its `aud` must include this service",
            )
        call.set_defaults(run=run)

    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
