---
type: C4 Component
title: Node API client
status: stable
groma:
  id: api
  parent: ui
  code:
    - scanner: javascript
      file: src/circuless_node/ui/assets/api.js
description: The UI's calls to its own node, with the token attached.
---

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [src/circuless_node/ui/assets/api.js](../../../../../../src/circuless_node/ui/assets/api.js) | [src/circuless_node/app.py](../../../../../../src/circuless_node/app.py) | Calls the node API | fetch, HTTPS |
