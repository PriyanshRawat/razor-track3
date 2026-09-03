"""The four Core tables of the Phase 1 spine, and the constraints that carry weight.

One ``MetaData`` is the single source of truth for both the SQLite the tests run on
and the Postgres ``schema_export`` emits. Where the two dialects differ, a
``with_variant`` records the difference in one place rather than in two schemas that
can drift.

Each table keeps the queryable columns a spine operation reads *plus* a ``data``
column holding the frozen contract model as JSON. The JSON is the source of truth on
read (``codec.decode_model``); the columns exist so a query does not have to parse
JSON, and so a reviewer can read the ledger with plain SQL.

The load-bearing constraints -- and the reason each exists -- are:

* ``outbox.idempotency_key`` UNIQUE -- one action, one row. Re-enqueueing the same
  proposal is a no-op, which is what makes the outbox *idempotent* rather than just a
  queue.
* ``uq_outbox_obligation_attempt`` -- a partial UNIQUE over
  ``(obligation_id, attempt_sequence)`` for debit rows. This is the constraint
  CONTRACTS.md Q1 (the highest-risk open hole) says Phase 1 must add: two *different*
  cases on one obligation derive different idempotency keys, so the key alone does
  not stop them both scheduling a debit. This does. It is partial because only debit
  rows carry an ``obligation_id``.
* ``uq_risk_case_active_obligation`` -- a partial UNIQUE over ``obligation_id`` for
  non-terminal cases, so the ledger holds at most one *live* case per obligation
  (§4/§13, "one row per obligation, no double counting"). Terminal cases are exempt,
  so an obligation may be re-opened after a case closes.
* ``audit_log.sequence`` PRIMARY KEY -- the monotonic row number the frozen
  ``AuditRow`` already hashes (JC-27); as a PK it also makes a concurrent double-append
  a key collision rather than a silent fork.
"""

from __future__ import annotations

import sqlalchemy as sa

from reclaim.contracts.enums import TERMINAL_CASE_STATES

metadata = sa.MetaData()

#: BIGINT on Postgres, INTEGER on SQLite. SQLite only auto-increments an INTEGER
#: PRIMARY KEY (its rowid alias); a BIGINT PK would not.
_AutoBigInt = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

#: The terminal states, as raw string values, for the partial-index predicates.
#: Derived from the frozen enum so the SQL cannot silently disagree with the
#: contract; a guard test walks both.
TERMINAL_STATE_VALUES: tuple[str, ...] = tuple(
    sorted(state.value for state in TERMINAL_CASE_STATES)
)


obligations = sa.Table(
    "obligations",
    metadata,
    sa.Column("obligation_id", sa.Text, primary_key=True),
    sa.Column("payer_id", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("currency", sa.Text, nullable=False),
    sa.Column("gross_amount_paise", sa.BigInteger, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("due_at", sa.Text, nullable=False),
    sa.Column("data", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
)


risk_cases = sa.Table(
    "risk_cases",
    metadata,
    sa.Column("case_id", sa.Text, primary_key=True),
    sa.Column(
        "obligation_id",
        sa.Text,
        sa.ForeignKey("obligations.obligation_id"),
        nullable=False,
    ),
    sa.Column("payer_id", sa.Text, nullable=False),
    sa.Column("arm", sa.Text, nullable=False),
    sa.Column("segment", sa.Text, nullable=False),
    sa.Column("risk_class", sa.Text, nullable=False),
    sa.Column("amount_at_risk_paise", sa.BigInteger, nullable=False),
    sa.Column("currency", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("stratum_key", sa.Text, nullable=False),
    sa.Column("detected_at", sa.Text, nullable=False),
    sa.Column("recovery_window_ends_at", sa.Text, nullable=False),
    sa.Column("stop_reason", sa.Text),
    sa.Column("stopped_at", sa.Text),
    sa.Column("data", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.Index(
        "uq_risk_case_active_obligation",
        "obligation_id",
        unique=True,
        sqlite_where=sa.text(
            "state NOT IN ("
            + ", ".join(f"'{v}'" for v in TERMINAL_STATE_VALUES)
            + ")"
        ),
        postgresql_where=sa.text(
            "state NOT IN ("
            + ", ".join(f"'{v}'" for v in TERMINAL_STATE_VALUES)
            + ")"
        ),
    ),
)


outbox = sa.Table(
    "outbox",
    metadata,
    sa.Column("id", _AutoBigInt, primary_key=True, autoincrement=True),
    sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
    sa.Column("case_id", sa.Text, nullable=False),
    sa.Column("obligation_id", sa.Text),
    sa.Column("attempt_sequence", sa.Integer),
    sa.Column("action_type", sa.Text, nullable=False),
    sa.Column("envelope", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="pending"),
    sa.Column("claimed_by", sa.Text),
    sa.Column("claimed_at", sa.Text),
    sa.Column("completed_at", sa.Text),
    sa.Column("result_digest", sa.Text),
    sa.Column("error", sa.Text),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.Index(
        "uq_outbox_obligation_attempt",
        "obligation_id",
        "attempt_sequence",
        unique=True,
        sqlite_where=sa.text("obligation_id IS NOT NULL"),
        postgresql_where=sa.text("obligation_id IS NOT NULL"),
    ),
)


audit_log = sa.Table(
    "audit_log",
    metadata,
    sa.Column("sequence", sa.BigInteger, primary_key=True, autoincrement=False),
    sa.Column("ts", sa.Text, nullable=False),
    sa.Column("case_id", sa.Text),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("idempotency_key", sa.Text),
    sa.Column("prev_hash", sa.Text, nullable=False),
    sa.Column("row_hash", sa.Text, nullable=False, unique=True),
    sa.Column("data", sa.Text, nullable=False),
)
