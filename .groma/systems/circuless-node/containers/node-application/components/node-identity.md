---
type: C4 Component
title: Node identity
status: stable
groma:
  id: node-identity
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/identity.py
  group: Identity and authorisation
  technology: private_key_jwt, X.509
description: How the node proves to Keycloak that it is itself.
---

On first start the node generates its own keypair and wraps the public half in a self-signed certificate. Only the certificate is ever sent anywhere; the private key never leaves the host, and it is excluded from every platform backup by design.

Authentication is `private_key_jwt`: the node signs a short-lived assertion rather than presenting a secret, so there is no shared credential to leak. Keycloak holds the certificate that actually authenticates; a fingerprint recorded anywhere else is reference only.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [src/circuless_node/identity.py](../../../../../../src/circuless_node/identity.py) | [keycloak](../../../../../externals/keycloak.md) | Authenticates as itself | private_key_jwt |
