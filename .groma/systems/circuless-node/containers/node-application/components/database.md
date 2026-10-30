---
type: C4 Component
title: Database and schema
status: stable
groma:
  id: database
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/db.py
    - scanner: python
      file: src/circuless_node/models.py
    - scanner: python
      file: migrations/env.py
    - scanner: python
      file: migrations/versions/15e0f9bece1b_sync_cache.py
    - scanner: python
      file: migrations/versions/321e3a0701ad_service_credential.py
    - scanner: python
      file: migrations/versions/3778de9637e2_resource_registry.py
    - scanner: python
      file: migrations/versions/457c943585b3_initial_tenant.py
    - scanner: python
      file: migrations/versions/49f96a357f4c_access_log.py
    - scanner: python
      file: migrations/versions/73687c4f90a9_node_identity.py
  group: Runtime
  technology: SQLModel, SQLAlchemy, Alembic, SQLite or PostgreSQL
description: 'The node''s own store: the models, the engine, and the migrations that shape it.'
---

SQLite with write-ahead logging in the usual deployment, PostgreSQL where one is available. Alembic from the first model, so there has never been a schema that was not a migration.

The migrations do more than create tables. The access log's append-only behaviour is a **database trigger** rather than a convention in the application: the node owns the table and a table owner bypasses grants, so the thing being defended against is the project's own next bug. The SQLite trigger has to name every immutable column, and a test updates each column of the live model in turn to catch that list going stale.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [src/circuless_node/db.py](../../../../../../src/circuless_node/db.py) | [src/circuless_node/tenancy.py](../../../../../../src/circuless_node/tenancy.py) | Filters by tenant | SQLAlchemy loader criteria |
