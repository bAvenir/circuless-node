---
type: C4 Component
title: HTTP application
status: stable
groma:
  id: http-application
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/app.py
    - scanner: python
      file: src/circuless_node/errors.py
    - scanner: python
      file: src/circuless_node/__init__.py
  group: Runtime
  technology: FastAPI
description: Builds the two applications, mounts the routes, and turns every failure into a reason code.
---

Creates the public application and the internal one, attaches the routers to each, and installs the error handlers and the request id that both carry.

Errors leave as a status code plus a reason code from **one enumeration** — never a stack trace, an internal path or a version. New reason codes are added to the enumeration rather than written as free text at the point of failure. An upstream service's own error body is passed through on an invoke and nowhere else.

The request id is outermost, so even the 401s and 403s carry it — those are the responses somebody is most often holding when they ask what happened.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [src/circuless_node/app.py](../../../../../../src/circuless_node/app.py) | [src/circuless_node/auth.py](../../../../../../src/circuless_node/auth.py) | Verifies every request | FastAPI dependency |
| [src/circuless_node/app.py](../../../../../../src/circuless_node/app.py) | [src/circuless_node/ui_staging.py](../../../../../../src/circuless_node/ui_staging.py) | Serves the browser UI | Static files |
