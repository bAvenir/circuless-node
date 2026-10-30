---
type: C4 Component
title: Client example
status: stable
groma:
  id: act
  parent: worked-examples
  code:
    - scanner: python
      file: examples/acting-for-a-user/act.py
description: Drives the three ways an application can call a node, printing the claims of each token.
---

As a browser would, as the application itself, and acting for a signed-in person through token exchange. The point is the difference in what the node sees and therefore what the access log can tell you afterwards.
