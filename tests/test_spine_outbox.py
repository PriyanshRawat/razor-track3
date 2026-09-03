"""Phase 1 spine: the idempotent outbox.

The outbox is where a proposed action waits to be executed exactly once. Two
independent guarantees, each pinned here:

* **Idempotent on the action.** ``ActionEnvelope.idempotency_key`` is derived from the
  case and parameters, and the column is UNIQUE -- re-enqueueing the same proposal is
  a no-op that returns the existing row (invariant #8).
* **Q1: no cross-case double debit.** Two *different* cases on one obligation derive
  *different* idempotency keys, so the key alone does not stop them both scheduling a
  debit. A partial UNIQUE index on ``(obligation_id, attempt_sequence)`` does. The
  cross-case test below is the one CONTRACTS.md flagged as the highest-risk open hole,
  and the last test proves the index itself, not just the friendly pre-check.

The claim/mark_done/mark_failed trio is the minimal worker lifecycle. This module is
deliberately *not* wired to the audit log: recording ``action_executed`` rows is the
executor's job in a later step, not the queue's.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from reclaim.contracts.canonical import digest
from reclaim.spine import outbox
from reclaim.spine.errors import DoubleDebitBlocked
from reclaim.spine.tables import outbox as outbox_table

STAMP = "2026-08-01T09:00:00.000000Z"


def test_enqueue_persists_a_pending_row(conn, make_debit_envelope):
    item = outbox.enqueue(conn, make_debit_envelope())
    assert item.status == "pending"
    assert item.idempotency_key == make_debit_envelope().idempotency_key
    assert outbox.get(conn, item.id).status == "pending"


def test_enqueue_is_idempotent_on_the_key(conn, make_debit_envelope):
    first = outbox.enqueue(conn, make_debit_envelope())
    second = outbox.enqueue(conn, make_debit_envelope())  # identical proposal
    assert first.id == second.id
    count = conn.execute(
        outbox_table.select().with_only_columns(outbox_table.c.id)
    ).fetchall()
    assert len(count) == 1


def test_q1_blocks_a_cross_case_double_debit(conn, make_debit_envelope):
    outbox.enqueue(
        conn, make_debit_envelope(case_id="case_1", action_id="act_1", attempt_sequence=1)
    )
    # A different case on the SAME obligation and attempt: a different idempotency
    # key, so only the (obligation, attempt) constraint can catch it.
    with pytest.raises(DoubleDebitBlocked):
        outbox.enqueue(
            conn,
            make_debit_envelope(case_id="case_2", action_id="act_2", attempt_sequence=1),
        )


def test_a_new_attempt_sequence_is_allowed_on_the_same_obligation(
    conn, make_debit_envelope
):
    outbox.enqueue(
        conn, make_debit_envelope(case_id="case_1", action_id="act_1", attempt_sequence=1)
    )
    outbox.enqueue(
        conn, make_debit_envelope(case_id="case_1", action_id="act_2", attempt_sequence=2)
    )
    rows = conn.execute(
        outbox_table.select().with_only_columns(outbox_table.c.id)
    ).fetchall()
    assert len(rows) == 2


def test_the_double_debit_index_is_enforced_by_the_database(engine, make_debit_envelope):
    """Bypass the pre-check and the key: two debit rows for one
    ``(obligation, attempt_sequence)`` must be rejected by the partial UNIQUE index."""
    with engine.begin() as c:
        outbox.enqueue(c, make_debit_envelope(attempt_sequence=1))

    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(
                outbox_table.insert().values(
                    idempotency_key="a-different-key-entirely",
                    case_id="case_2",
                    obligation_id="obl_1",
                    attempt_sequence=1,
                    action_type="schedule_debit",
                    envelope="{}",
                    status="pending",
                    created_at=STAMP,
                    updated_at=STAMP,
                )
            )


def test_claim_marks_the_row_and_then_the_queue_is_empty(conn, make_debit_envelope):
    item = outbox.enqueue(conn, make_debit_envelope())
    claimed = outbox.claim(conn, worker="worker-1")
    assert claimed.id == item.id
    assert claimed.status == "claimed"
    assert claimed.claimed_by == "worker-1"
    assert outbox.claim(conn, worker="worker-1") is None


def test_mark_done_completes_a_claimed_row(conn, make_debit_envelope):
    item = outbox.enqueue(conn, make_debit_envelope())
    outbox.claim(conn, worker="worker-1")
    result = digest({"psp": "accepted"})
    outbox.mark_done(conn, item.id, result_digest=result)
    done = outbox.get(conn, item.id)
    assert done.status == "done"
    assert done.result_digest == result


def test_mark_failed_records_the_error(conn, make_debit_envelope):
    item = outbox.enqueue(conn, make_debit_envelope())
    outbox.claim(conn, worker="worker-1")
    outbox.mark_failed(conn, item.id, error="psp timeout")
    failed = outbox.get(conn, item.id)
    assert failed.status == "failed"
    assert failed.error == "psp timeout"
