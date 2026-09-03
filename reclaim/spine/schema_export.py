"""Postgres DDL export for the spine (Phase 1).

Production is Postgres (§20); the tests run on SQLite. Rather than maintain a
hand-written ``schema.sql`` beside the ``MetaData`` -- two schemas that drift -- this
compiles the one ``MetaData`` under the Postgres dialect, with no driver and no live
connection. ``python -m reclaim.spine.schema_export`` prints the DDL, which is how the
checked-in ``schema.sql`` is regenerated.

Tables come out in dependency order (``sorted_tables``) so the script runs top to
bottom against an empty database; each table's indexes follow it, sorted by name so
the output is stable across runs.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from reclaim.spine.tables import metadata

__all__ = ["postgres_ddl"]


def postgres_ddl() -> str:
    """The full Postgres DDL for every spine table and index, as one string."""
    dialect = postgresql.dialect()
    statements: list[str] = []
    for table in metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for index in sorted(table.indexes, key=lambda ix: ix.name or ""):
            statements.append(
                str(CreateIndex(index).compile(dialect=dialect)).strip() + ";"
            )
    return "\n\n".join(statements) + "\n"


if __name__ == "__main__":  # pragma: no cover - regenerates schema.sql
    print(postgres_ddl())
