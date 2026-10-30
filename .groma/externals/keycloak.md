---
type: C4 System
title: Keycloak
status: stable
groma:
  id: keycloak
  technology: OpenID Connect, JWKS
---

The platform's single identity provider. Every caller presents a token it issued; the node verifies the signature against its JWKS, checks the issuer and that the audience is exactly this node, and resolves the caller's organisations from the token's groups. The node authenticates to it as itself with a signed assertion, holding no client secret.
