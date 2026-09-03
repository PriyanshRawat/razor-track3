"""Phase 1 spine: the Postgres DDL export.

Production is Postgres (§20); the tests run on SQLite. ``postgres_ddl`` compiles the
one ``MetaData`` under the Postgres dialect -- no driver, no live database -- so the
``schema.sql`` a reviewer reads is generated from the exact tables the tests exercise,
not hand-maintained beside them. What must hold: all four tables appear, and both
load-bearing partial UNIQUE indexes appear *with their predicates* (an index emitted
without its ``WHERE`` would silently become total and forbid legitimate rows).
"""

from __future__ import annotations

from reclaim.spine import schema_export


def test_postgres_ddl_names_all_four_tables():
    ddl = schema_export.postgres_ddl()
    for table in ("obligations", "risk_cases", "outbox", "audit_log"):
        assert f"CREATE TABLE {table}" in ddl


def test_postgres_ddl_emits_the_q1_partial_index_with_its_predicate():
    ddl = schema_export.postgres_ddl()
    assert "uq_outbox_obligation_attempt" in ddl
    assert "obligation_id IS NOT NULL" in ddl


def test_postgres_ddl_emits_the_one_live_case_index_with_its_predicate():
    ddl = schema_export.postgres_ddl()
    assert "uq_risk_case_active_obligation" in ddl
    assert "state NOT IN" in ddl


def test_postgres_ddl_uses_bigint_not_the_sqlite_integer_variant():
    # The PG dialect must render the BigInteger side of the with_variant, not the
    # SQLite Integer it falls back to in tests.
    ddl = schema_export.postgres_ddl()
    assert "BIGINT" in ddl or "BIGSERIAL" in ddl
