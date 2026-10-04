"""A database built by the migrations, rather than by `create_all`.

Almost every test here builds its schema with `SQLModel.metadata.create_all`, which is
fast and perfectly adequate for testing behaviour that lives in Python. It has one blind
spot, and N11 landed squarely in it: **`create_all` creates tables, not triggers.**

So a test suite that only ever used `create_all` could assert that the access log is
append-only, pass, and be testing nothing at all — the guarantee is a trigger in
`49f96a357f4c`, and in a `create_all` database it simply is not there. That is the same
shape of failure as the three route-enumeration bugs: a security gate that is vacuous
rather than wrong, and therefore green.

Running the real migrations costs about a second, which is why it is here rather than in
`conftest` for everything. Use it for anything whose guarantee lives in the schema.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from circuless_node.db import create_db_engine
from circuless_node.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


def migrated_engine(settings: Settings) -> Engine:
    """Upgrade this settings object's database to head and return an engine on it.

    `env.py` reads the URL from `Settings`, which reads `CIRCULESS_NODE_DATABASE_URL` —
    so the variable is set for the duration of the upgrade rather than the URL being
    passed in, which keeps one source of truth for where the database is.
    """
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))

    previous = os.environ.get("CIRCULESS_NODE_DATABASE_URL")
    os.environ["CIRCULESS_NODE_DATABASE_URL"] = settings.database_url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("CIRCULESS_NODE_DATABASE_URL", None)
        else:
            os.environ["CIRCULESS_NODE_DATABASE_URL"] = previous

    return create_db_engine(settings)
