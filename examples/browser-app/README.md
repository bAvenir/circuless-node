# A partner application, in one file

```sh
python3 -m http.server 5173
open http://127.0.0.1:5173/
```

> Not the node's admin UI — that is N13, inside the node. This is an example of what a
> **partner** builds on top of CIRCULess.

No framework and no build step, so the whole thing can be read in one sitting. The parts
worth reading are `signIn()` and `tokenFor()`; everything else is ordinary `fetch`.

## What it does

Signs a person in, lists a tenant's resources, uploads and downloads a CSV, and has the
reference service extract its column names — the scenario end to end, from a browser.

## Why port 5173

`circuless-ui` already registers `http://127.0.0.1:5173/*` as a redirect URI **and** as a
web origin, so this runs against the realm as deployed with no change to it. The page
uses its own URL as the redirect target, so a static file server is all it needs.

## The bit that is not obvious

**One sign-in, one access token per audience.**

A token is valid for exactly one audience — a token two systems accept is replayable
between them — so an application touching the catalogue *and* a node needs two. It gets
them by requesting every audience at sign-in and then narrowing, per audience, through
the refresh grant.

Two consequences, both of which the page shows rather than describes:

**The access token from sign-in is useless.** It carries every audience that was
requested, so every API rejects it. The page keeps the refresh token, discards that
access token, and prints its `aud` on screen in red — the mistake of using it is easy to
make and produces a 401 with a token that looks perfectly valid.

**A refresh cannot widen.** Anything that might be needed later has to be requested at
sign-in, or the person signs in again.

Renewal by hidden iframe is avoided on purpose: the identity provider is on another
origin, so it would depend on third-party cookies, which browsers increasingly block.

Tokens are held in memory, never in `localStorage`. The refresh token is the
longest-lived credential here.

## Before it will work

| | |
|---|---|
| the node's permitted browser origins | must include `http://127.0.0.1:5173`. Every call is cross-origin with a bearer token, and wildcards are refused. A CORS failure appears in the console as an opaque network error, not a 403 |
| a dataset and a service resource | the ids go in the form; `List resources` finds them |
| an agreement | if the data and the service belong to different organisations |

## Verification

Hand-run against a node. There is no automated coverage of this page: browser
automation is out of scope for the node's test suite, and what sits behind it — the
proxy, the credential, the agreement, the access log — is covered by
`tests/test_reference_service.py`.

What has been checked mechanically: the JavaScript parses, and the page's PKCE
challenge is byte-identical to the reference implementation's for the same verifier.
