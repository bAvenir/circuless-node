---
type: C4 Component
title: Browser sign-in
status: stable
groma:
  id: session
  parent: ui
  code:
    - scanner: javascript
      file: src/circuless_node/ui/assets/session.js
  technology: OAuth2 Authorization Code with PKCE
description: Gets a token for this node in the browser, without a client secret.
---

Reads the four-key `config.json` the node writes at startup, runs the code exchange with PKCE, and narrows the result to this node's audience. A public client in a browser cannot keep a secret, which is why PKCE rather than one.
