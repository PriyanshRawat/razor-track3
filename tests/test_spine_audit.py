"""Phase 1 spine: the append-only audit store.

Per the scope decision, this reuses the frozen ``AuditRow`` and ``append_row`` -- so
sequence numbering and ``prev_hash`` linkage come for free -- and does *not* add a
``verify_chain`` CLI or any hardening. What it must prove: rows are numbered from
zero, each points at its predecessor, and a stored row reads back as its exact,
self-verifying equal.
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from reclaim.contracts.audit import GENESIS_HASH
from reclaim.contracts.canonical import digest
from reclaim.contracts.enums import ActorType
from reclaim.spine import audit_store
from reclaim.spine.tables import audit_log

TS = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)
DIGEST = digest({"observed": "case_opened"})


def _append(conn, **over):
    fields = dict(
        ts=TS,
        actor=ActorType.SYSTEM,
        event_type="case_opened",
        inputs_digest=DIGEST,
        decision_rationale="detector opened the case",
    )
    fields.update(over)
    return audit_store.append(conn, **fields)


def test_first_appended_row_is_the_genesis_row(conn):
    row = _append(conn)
    assert row.sequence == 0
    assert row.prev_hash == GENESIS_HASH


def test_appended_rows_link_into_a_chain(conn):
    first = _append(conn)
    second = _append(conn, event_type="case_state_changed")
    assert second.sequence == 1
    assert second.prev_hash == first.row_hash


def test_a_stored_row_reads_back_equal(conn):
    appended = _append(conn)
    assert audit_store.tail(conn) == appended


def test_read_all_returns_rows_in_sequence_order(conn):
    _append(conn)
    _append(conn, event_type="case_state_changed")
    _append(conn, event_type="case_recovered")
    rows = audit_store.read_all(conn)
    assert [r.sequence for r in rows] == [0, 1, 2]


def test_append_persists_exactly_one_row_per_call(conn):
    _append(conn)
    _append(conn, event_type="case_state_changed")
    count = conn.execute(sa.select(sa.func.count()).select_from(audit_log)).scalar_one()
    assert count == 2
