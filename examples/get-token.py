#!/usr/bin/env python3
"""Get an access token for the Cloud API, the way a real user would.

    ./get-token.py --issuer https://auth.circuless.bavenir.eu/realms/circuless

Prints the token on stdout and its claims on stderr, so it composes:

    TOKEN=$(./get-token.py --issuer https://auth.../realms/circuless)
    ./verify-cloud-api.py --base-url https://api.circuless.bavenir.eu --token "$TOKEN"

## Why there is no one-line curl for this

Password grants are **off on every client** (SR-1.1.4, H2), deliberately, so nothing can
trade a username and password for a token. The only way in is the authorization code flow
with PKCE — which needs a browser, because it is a login page.

That is the whole point of the control, so this drives the real flow rather than working
around it: it opens the browser, catches the redirect on a loopback listener, and
exchanges the code. No realm change, no temporary client, no credentials on a command
line, and the token it produces is exactly the one the Cloud UI will carry.

`circuless-ui` already registers `http://127.0.0.1:5173/*` as a redirect URI — the Vite
port the UI will use in M4 — so the listener below is a URI Keycloak already trusts.

## Run it where the browser is

Usually your laptop, not the server: Keycloak is publicly reachable, and the redirect only
has to reach *this* process. Over SSH with no browser, use `--no-browser` and open the
printed URL yourself; the listener still has to be on the machine the browser can reach.

Standard library only, like `kc.py`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

#: Registered on `circuless-ui` in realm-config.json. Changing it needs a realm change.
REDIRECT_PORT = 5173

def http_open(request, timeout: int = 30):
    """`urlopen`, with the scheme checked first.

    Bandit flags a bare `urlopen` (B310) because urllib will cheerfully open `file://`
    and `ftp://` too. Every URL here is built from the issuer the caller passed rather
    than from arbitrary input, so the risk is theoretical — but asserting it costs one
    line and turns a blanket suppression into an actual check.

    The same wrapper is in `kc.py`, `dev_keypair.py` and `spike_token_exchange.py`.
    This script was the one that did not have it, which is why Bandit failed CI.
    """
    url = request if isinstance(request, str) else request.full_url
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing to open a non-HTTP URL: {url}")
    return urllib.request.urlopen(request, timeout=timeout)  # nosec B310 — checked above


REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"

#: `circuless-cloud` is the audience the Cloud API accepts and nothing else (invariant 2).
#: It is a realm default *optional* scope, so it has to be asked for by name.
DEFAULT_SCOPE = "openid circuless-cloud"


class CodeCatcher(http.server.BaseHTTPRequestHandler):
    """A one-request web server whose only job is to read the `code` query parameter."""

    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's interface
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        CodeCatcher.code = query.get("code", [None])[0]
        CodeCatcher.error = query.get("error_description", query.get("error", [None]))[0]

        body = b"Signed in. You can close this tab and go back to the terminal."
        if CodeCatcher.error:
            body = f"Sign-in failed: {CodeCatcher.error}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        """Silence the default access log: stdout is the token and nothing else."""


def claims_of(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))



def warnings_for(claims: dict) -> list[str]:
    """What is wrong with this token, before the caller discovers it as a 401.

    Pure, and separate from `main`, because `main` needs a browser and a realm. These
    three are everything a token can be missing that the API will refuse it for, and
    each one is a question somebody has already had to ask.
    """
    found = []

    if "circuless-cloud" not in json.dumps(claims.get("aud")):
        found.append(
            "  WARNING: aud does not include circuless-cloud; the API will "
            "reject this (invariant 2)"
        )

    # Printed above as `None`, which is easy to read past. Keycloak's User Profile has no
    # mechanism for attribute defaults, so an attribute nobody set produces no claim, and
    # the API refuses the token rather than assuming `user` — deliberately, because a node
    # whose attribute was never set would otherwise be read as a user and pass the check
    # meant to reject it (D14). Every call fails until an admin sets it, and nothing else
    # says where to go.
    if not claims.get("principal_type"):
        found.append(
            "  WARNING: no principal_type claim. Every API call will be refused with\n"
            '    {"reason":"invalid_token","detail":"token carries no principal_type"}\n'
            "  Set it on the user, admin-side: Users -> Attributes -> principal_type = user\n"
            "  (admin-editable only, by design — see deploy/keycloak/README.md)"
        )

    # Not an error and usually not wrong — most callers are ordinary members — but the
    # registries and the audit log are platform-admin only, so a 403 from /v1/orgs is
    # this rather than a bug.
    if "platform-admin" not in claims.get("realm_access", {}).get("roles", []):
        found.append(
            "  note: not a platform operator. /v1/orgs, /v1/nodes and /v1/audit will\n"
            "  answer 403 — assign the realm role platform-admin to use them."
        )

    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issuer", required=True, help="https://<host>/realms/circuless")
    parser.add_argument("--client-id", default="circuless-ui")
    parser.add_argument(
        "--scope",
        default=DEFAULT_SCOPE,
        help="use 'openid node:bvr-cloud' to get a node-audienced token instead",
    )
    parser.add_argument("--no-browser", action="store_true", help="print the URL instead")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    issuer = args.issuer.rstrip("/")

    # PKCE: a fresh secret per run, sent only as its SHA-256 up front. A public client has
    # no secret, so this is what stops an intercepted code being redeemed by anyone else.
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    state = secrets.token_urlsafe(16)

    authorize = f"{issuer}/protocol/openid-connect/auth?" + urllib.parse.urlencode(
        {
            "client_id": args.client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": args.scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )

    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), CodeCatcher)
    server.timeout = args.timeout
    threading.Thread(target=server.handle_request, daemon=True).start()

    print(f"waiting for sign-in on {REDIRECT_URI} …", file=sys.stderr)
    if args.no_browser or not webbrowser.open(authorize):
        print(f"\nopen this in a browser:\n\n{authorize}\n", file=sys.stderr)

    for _ in range(args.timeout * 10):
        if CodeCatcher.code or CodeCatcher.error:
            break
        threading.Event().wait(0.1)
    server.server_close()

    if CodeCatcher.error:
        print(f"sign-in failed: {CodeCatcher.error}", file=sys.stderr)
        return 1
    if not CodeCatcher.code:
        print(f"no code received within {args.timeout}s", file=sys.stderr)
        return 1

    request = urllib.request.Request(
        f"{issuer}/protocol/openid-connect/token",
        data=urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "client_id": args.client_id,
                "code": CodeCatcher.code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            }
        ).encode(),
        method="POST",
    )
    try:
        with http_open(request, timeout=30) as response:
            token = json.load(response)["access_token"]
    except urllib.error.HTTPError as refused:
        # The body carries `error_description`, which is the whole diagnosis — a bare
        # status code here sends people to the wrong place.
        print(f"token endpoint refused: {refused.code} {refused.read().decode()}", file=sys.stderr)
        return 1

    claims = claims_of(token)
    print(f"\n  aud            {claims.get('aud')}", file=sys.stderr)
    print(f"  sub            {claims.get('sub')}", file=sys.stderr)
    print(f"  principal_type {claims.get('principal_type')}", file=sys.stderr)
    print(f"  groups         {claims.get('groups')}", file=sys.stderr)
    print(f"  expires in     {claims.get('exp', 0) - claims.get('iat', 0)}s\n", file=sys.stderr)
    for warning in warnings_for(claims):
        print(f"{warning}\n", file=sys.stderr)

    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
