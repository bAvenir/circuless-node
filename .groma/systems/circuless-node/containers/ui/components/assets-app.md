---
type: C4 Component
title: Admin views
status: stable
groma:
  id: assets-app
  parent: ui
  code:
    - scanner: javascript
      file: src/circuless_node/ui/assets/app.js
description: 'The screens: tenants, resources, a resource''s detail, and the access log.'
---

Distinguishes membership from administration throughout — a member of an organisation sees its tenant but not a link into resources they may not manage, because a link that leads to a refusal is worse than no link.
