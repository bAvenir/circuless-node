---
type: C4 Component
title: Token verification and subject resolution
status: stable
groma:
  id: token-verification
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/auth.py
    - scanner: python
      file: src/circuless_node/jwks.py
    - scanner: python
      file: src/circuless_node/oidc.py
      symbol: discover
    - scanner: python
      file: src/circuless_node/subject.py
  group: Identity and authorisation
  technology: PyJWT, JWKS
description: Turns a bearer token into a trusted subject, or refuses it.
---

Checks, in this order: the signature against the cached JWKS, refetching once on an unknown key; the issuer; that the audience is **exactly** this node and no other; and the clock, with sixty seconds of leeway.

Then it resolves who that is. Organisations come only from depth-one `/orgs/<x>` groups, administration from `/orgs/<x>/admins`; every other group in the realm is ignored. A service principal gets its organisation from a claim instead, and its groups are ignored entirely — a service account dropped into an admins group does not become an admin.

Identity comes from the token and never from the request body. Where several of a caller's organisations could authorise a request, the caller must name which one, and that is checked against membership.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [src/circuless_node/auth.py](../../../../../../src/circuless_node/auth.py) | [keycloak](../../../../../externals/keycloak.md) | Verifies tokens | JWKS over HTTPS |
| [src/circuless_node/auth.py](../../../../../../src/circuless_node/auth.py) | [src/circuless_node/tenancy.py](../../../../../../src/circuless_node/tenancy.py) | Scopes to the tenant | Python call |
