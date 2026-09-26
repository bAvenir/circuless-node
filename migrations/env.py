"""Alembic environment.

The database URL comes from the node's own settings, not from alembic.ini, so migrations
and the running node can never disagree about which database they mean.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

from circuless_node import models  # noqa: F401  — imported for its side effect: table registration
from circuless_node.db import create_db_engine
from circuless_node.settings import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def _settings() -> Settings:
    # node_id is required at runtime but irrelevant to a migration, so a placeholder keeps
    # `alembic upgrade` usable on a host that has not been configured yet.
    return Settings(node_id="migration")  # type: ignore[call-arg]


def run_migrations_offline() -> None:
    context.configure(
        url=_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things in place; batch mode rewrites the table instead.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_settings())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
