"""RECLAIM Phase 1 -- the persistence spine.

Phase 0 (``reclaim.contracts``) is pure schema with no I/O; a contract test forbids
it from importing ``os``, ``sqlite3`` or SQLAlchemy. This package is the other side
of that line: it is where the frozen contracts are given a database. It therefore
lives *outside* ``reclaim.contracts`` on purpose, and nothing here is imported by a
contract.

What it contains, and nothing more (Phase 1 core spine):

* ``db``          -- engine construction from ``RECLAIM_DATABASE_URL`` (Postgres in
                     production, in-memory SQLite in the test suite).
* ``tables``      -- the four Core tables: obligations, the risk-case ledger, the
                     idempotent outbox, and the append-only audit log.
* ``codec``       -- lossless encode/decode between a frozen contract model and its
                     stored JSON.
* ``ledger``      -- the Revenue-at-Risk ledger: open a case, read it back, list
                     what is still at risk.
* ``case_machine``-- the ``§9.1`` state machine: a transition is checked against the
                     frozen ``ALLOWED_CASE_TRANSITIONS`` and writes the new state and
                     its audit row in one transaction.
* ``outbox``      -- enqueue/claim/complete an action idempotently.
* ``audit_store`` -- append rows via the frozen ``audit.append_row``.

Deliberately absent: detectors, the policy engine, the planner/diagnostician, the
scheduler, reconciliation, any hash-chain *verification* CLI, and any UI.
"""
