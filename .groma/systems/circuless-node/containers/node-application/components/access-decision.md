---
type: C4 Component
title: Access decision
status: stable
groma:
  id: access-decision
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/decide.py
    - scanner: python
      file: src/circuless_node/policy.py
  group: Identity and authorisation
description: The one function that grants access, and the policy vocabulary it reads.
---

`decide(subject, action, resource, agreements, now)` is **pure**: no database, no clock, no network. Agreements and the time are passed in, and it returns a decision and a reason code. Handlers never re-implement a rule.

Withdrawn resources are refused to everyone. Private is the owning organisation's admins; org-visible is anyone in that organisation; agreement-visible needs an accepted agreement matching provider, acting consumer, resource, action and validity window; public is any authenticated caller — never anonymous, and never a node.

Because it is pure it has an exhaustive table test across every visibility, principal type, agreement state and time boundary, which is the real reason the rule can be trusted.
