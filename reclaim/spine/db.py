"""Engine construction for the spine.

Production uses Postgres via ``RECLAIM_DATABASE_URL`` (§20: "PostgreSQL only"). The
test suite uses in-memory SQLite, which is why the URL is resolvable three ways --
explicit argument, environment, then the SQLite default -- with the argument winning
so a test can be explicit regardless of the ambient environment.

In-memory SQLite is per-connection: a second connection sees an empty database. The
suite creates the schema once and then runs many operations, so a ``StaticPool``
pins a single underlying connection for the life of the engine. That is a test
convenience, not a production setting; a Postgres URL takes neither branch.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from reclaim.spine.tables import metadata

ENV_VAR = "RECLAIM_DATABASE_URL"
DEFAULT_URL = "sqlite://"


def get_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Build an engine. Explicit ``url`` > ``RECLAIM_DATABASE_URL`` > SQLite."""
    resolved = url or os.environ.get(ENV_VAR) or DEFAULT_URL
    kwargs: dict[str, object] = {"echo": echo}
    if resolved.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["poolclass"] = StaticPool
    engine = create_engine(resolved, **kwargs)
    if engine.dialect.name == "sqlite":
        # Foreign keys are off by default in SQLite; the ledger's obligation_id FK is
        # only meaningful with them on. Postgres enforces FKs unconditionally.
        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):  # pragma: no cover
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_all(engine: Engine) -> None:
    """Create every spine table. Idempotent (``checkfirst`` is on by default)."""
    metadata.create_all(engine)


def is_postgres(bind) -> bool:
    """True when the engine/connection speaks to Postgres.

    The one place the spine branches on dialect is the outbox claim: Postgres can
    ``SELECT ... FOR UPDATE SKIP LOCKED``; SQLite serialises writes and cannot.
    """
    return bind.dialect.name == "postgresql"
