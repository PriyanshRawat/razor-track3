"""The idempotent outbox (Phase 1 spine).

A proposed action lands here and is executed exactly once. Two guarantees:

* **Idempotent on the action.** ``idempotency_key`` (derived, invariant #8) is UNIQUE,
  so ``enqueue`` of an already-seen proposal is a no-op that returns the existing row.
* **Q1 -- no cross-case double debit.** Two different cases on one obligation derive
  *different* keys, so the key cannot stop them both debiting. The partial UNIQUE index
  ``uq_outbox_obligation_attempt`` over ``(obligation_id, attempt_sequence)`` does; only
  ``ScheduleDebit`` rows carry those coordinates, which is why the index is partial.
  ``enqueue`` also does a friendly pre-check that raises ``DoubleDebitBlocked`` before
  the insert; the index is the backstop for the concurrent race the pre-check cannot see.

``claim``/``mark_done``/``mark_failed`` are the worker lifecycle. On Postgres ``claim``
takes the row ``FOR UPDATE SKIP LOCKED`` so competing workers never grab the same one;
SQLite serialises writes and needs no such clause. The claim's ``WHERE status='pending'``
makes it a compare-and-set either way.

Stored the same way as the rest of the spine: queryable columns plus a lossless
``envelope`` JSON blob. The envelope is decoded with ``model_validate`` (not the codec)
so its claimed ``idempotency_key`` is *re-verified* on read -- a tamper check for free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from reclaim.contracts.actions import ActionEnvelope, ActionType
from reclaim.contracts.temporal import to_rfc3339, utcnow
from reclaim.spine.db import is_postgres
from reclaim.spine.errors import DoubleDebitBlocked
from reclaim.spine.tables import outbox as outbox_table

__all__ = [
    "OutboxItem",
    "STATUS_CLAIMED",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "claim",
    "enqueue",
    "get",
    "mark_done",
    "mark_failed",
]

STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class OutboxItem:
    """A read view of one outbox row. ``envelope`` is the re-verified action."""

    id: int
    idempotency_key: str
    case_id: str
    action_type: str
    status: str
    claimed_by: str | None
    result_digest: str | None
    error: str | None
    envelope: ActionEnvelope


def _debit_coordinates(envelope: ActionEnvelope) -> tuple[str | None, int | None]:
    """The Q1 constraint coordinates, present only for a debit."""
    action = envelope.action
    if action.action_type is ActionType.SCHEDULE_DEBIT:
        return action.obligation_id, action.attempt_sequence
    return None, None


def _row_to_item(row: sa.Row | Any) -> OutboxItem:
    m = row._mapping if hasattr(row, "_mapping") else row
    return OutboxItem(
        id=m["id"],
        idempotency_key=m["idempotency_key"],
        case_id=m["case_id"],
        action_type=m["action_type"],
        status=m["status"],
        claimed_by=m["claimed_by"],
        result_digest=m["result_digest"],
        error=m["error"],
        # Decoded via model_validate (not codec): keeping the claimed key lets
        # ActionEnvelope re-verify it against the parameters on read.
        envelope=ActionEnvelope.model_validate(json.loads(m["envelope"])),
    )


def get(conn: Connection, outbox_id: int) -> OutboxItem | None:
    """Read one outbox row back, or ``None`` if unknown."""
    row = conn.execute(
        outbox_table.select().where(outbox_table.c.id == outbox_id)
    ).first()
    return _row_to_item(row) if row is not None else None


def enqueue(
    conn: Connection, envelope: ActionEnvelope, *, at: Any = None
) -> OutboxItem:
    """Enqueue a proposed action. Idempotent on its key; Q1-guarded for debits."""
    key = envelope.idempotency_key

    existing = conn.execute(
        outbox_table.select().where(outbox_table.c.idempotency_key == key)
    ).first()
    if existing is not None:
        return _row_to_item(existing)

    obligation_id, attempt_sequence = _debit_coordinates(envelope)
    if obligation_id is not None:
        clash = conn.execute(
            sa.select(outbox_table.c.case_id).where(
                outbox_table.c.obligation_id == obligation_id,
                outbox_table.c.attempt_sequence == attempt_sequence,
            )
        ).first()
        if clash is not None:
            raise DoubleDebitBlocked(
                f"a debit for obligation {obligation_id!r} attempt "
                f"{attempt_sequence} is already enqueued (by case {clash[0]!r}); "
                "Q1 cross-case double-debit blocked"
            )

    stamp = to_rfc3339(at) if at is not None else to_rfc3339(utcnow())
    result = conn.execute(
        outbox_table.insert().values(
            idempotency_key=key,
            case_id=envelope.case_id,
            obligation_id=obligation_id,
            attempt_sequence=attempt_sequence,
            action_type=envelope.action.action_type.value,
            envelope=envelope.model_dump_json(),
            status=STATUS_PENDING,
            created_at=stamp,
            updated_at=stamp,
        )
    )
    return get(conn, result.inserted_primary_key[0])  # type: ignore[arg-type]


def claim(conn: Connection, *, worker: str, at: Any = None) -> OutboxItem | None:
    """Claim the oldest pending row for ``worker``, or ``None`` if the queue is empty."""
    pick = (
        sa.select(outbox_table.c.id)
        .where(outbox_table.c.status == STATUS_PENDING)
        .order_by(outbox_table.c.id.asc())
        .limit(1)
    )
    if is_postgres(conn):
        pick = pick.with_for_update(skip_locked=True)

    row_id = conn.execute(pick).scalar_one_or_none()
    if row_id is None:
        return None

    stamp = to_rfc3339(at) if at is not None else to_rfc3339(utcnow())
    conn.execute(
        outbox_table.update()
        .where(
            outbox_table.c.id == row_id,
            outbox_table.c.status == STATUS_PENDING,  # compare-and-set
        )
        .values(
            status=STATUS_CLAIMED,
            claimed_by=worker,
            claimed_at=stamp,
            updated_at=stamp,
        )
    )
    return get(conn, row_id)


def mark_done(
    conn: Connection, outbox_id: int, *, result_digest: str | None = None, at: Any = None
) -> None:
    """Mark a row done, recording the digest of the external result."""
    stamp = to_rfc3339(at) if at is not None else to_rfc3339(utcnow())
    conn.execute(
        outbox_table.update()
        .where(outbox_table.c.id == outbox_id)
        .values(
            status=STATUS_DONE,
            result_digest=result_digest,
            completed_at=stamp,
            updated_at=stamp,
        )
    )


def mark_failed(
    conn: Connection, outbox_id: int, *, error: str, at: Any = None
) -> None:
    """Mark a row failed, recording why."""
    stamp = to_rfc3339(at) if at is not None else to_rfc3339(utcnow())
    conn.execute(
        outbox_table.update()
        .where(outbox_table.c.id == outbox_id)
        .values(status=STATUS_FAILED, error=error, updated_at=stamp)
    )
