"""Stage two of two: removing what a withdrawal scheduled (N20, D25, invariant 14).

A `DELETE` marks a resource withdrawn and sets `purge_after`. This is what happens when
that date arrives — the bytes and the metadata go, and nothing else does.

## What survives, and why

**AccessLog entries stay.** `AccessLog.resource_id` is deliberately not a foreign key,
which is the whole reason it can outlive the row it names. An audit answers what
happened, not what is still there, and a retention policy that erased the record of who
read something would destroy the evidence rather than the data.

So after a purge, entries point at a resource that no longer exists. That is correct and
is asserted by a test, because it looks enough like a bug that somebody would otherwise
"fix" it.

## Order matters: bytes first, then the row

If the row went first and removing the bytes then failed, the files would be orphaned —
no record that they exist, nothing that will ever try again, and a retention promise
quietly broken by a full disk. Doing it the other way round, a failure leaves the
resource still marked withdrawn and still due, and the next pass retries. One ordering
is recoverable and the other is not.

## It crosses tenants, and says so

This is the job `tenancy.all_tenants()` was written for: a purge is about the node
rather than about one organisation, so it cannot be scoped to a tenant, and reaching
across them has to be deliberate rather than accidental.

## It runs beside the servers

Its own loop, a peer of the sync loop, for the same reason that one is: there is nothing
to schedule on a partner site and nothing to install. Hourly rather than every 30
seconds, because the shortest meaningful retention is a day and a purge is not urgent —
it is overdue or it is not.

`circuless-node purge` runs one pass on demand, which is how an operator forces it and
how the acceptance test reaches stage two without waiting a month.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from .models import Resource
from .storage import Storage
from .tenancy import all_tenants
from .vocabularies import ResourceStatus

#: An hour. Retention is measured in days, so checking more often buys nothing and a
#: purge that is late by minutes is not late.
PURGE_INTERVAL_SECONDS = 3600.0


def purge_due(engine: Engine, storage: Storage, now: dt.datetime | None = None) -> list[str]:
    """Remove every resource whose purge date has passed. Returns what went.

    `now` is a parameter for the same reason it is one in `decide()`: a test should not
    have to wait, and a job that reads the clock itself cannot be asked about a date.
    """
    moment = now or dt.datetime.now(dt.UTC)
    purged: list[str] = []

    with Session(engine) as session, all_tenants(session):
        due = session.exec(
            select(Resource).where(
                col(Resource.status) == ResourceStatus.WITHDRAWN,
                col(Resource.purge_after).is_not(None),
                col(Resource.purge_after) <= moment,
            )
        ).all()

        for resource in due:
            # Bytes first — see the module docstring. A failure here leaves the row
            # marked withdrawn and still due, so the next pass tries again.
            storage.purge_resource(resource.tenant_id, resource.id)
            session.delete(resource)
            # One transaction per resource, so a failure part-way through a backlog
            # keeps what it managed rather than rolling back a hundred successful
            # deletions because the hundred-and-first had a permissions problem.
            session.commit()
            purged.append(f"{resource.tenant_id}/{resource.id} ({resource.slug})")

    return purged


async def purge_loop(
    engine: Engine,
    storage: Storage,
    *,
    interval: float = PURGE_INTERVAL_SECONDS,
) -> None:
    """Run `purge_due` forever, beside the servers and the sync loop."""
    while True:
        try:
            purged = await asyncio.to_thread(purge_due, engine, storage)
            for line in purged:
                print(f"purged {line}", flush=True)
        except Exception as unexpected:  # noqa: BLE001
            # The loop has to survive anything. A node that stopped purging because one
            # resource had an unreadable directory would go on accumulating data it had
            # promised to delete, silently, until someone happened to look.
            print(f"purge pass failed: {unexpected}", flush=True)
        await asyncio.sleep(interval)
