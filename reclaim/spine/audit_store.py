"""The append-only audit log (Phase 1 spine).

Rows are the frozen ``reclaim.contracts.audit.AuditRow``, written through the frozen
``append_row``: ``sequence`` and ``prev_hash`` are derived from the tail of the
chain, never passed in, so an off-by-one is not expressible here any more than it is
in the contract.

Scope note (per the build decision): this is a plain append-only table that reuses
the contract's hash linkage. It intentionally ships no ``verify_chain`` CLI and no
tamper-response hardening -- those are a later step. The one integrity property that
comes for free is on *read*: ``AuditRow`` re-derives ``row_hash`` and rejects a
stored row whose contents no longer match it (JC-28), so ``tail``/``read_all`` cannot
return a silently edited row.

Every function takes a ``Connection``: the caller owns the transaction, which is how
a state change and its audit row commit together in ``case_machine``.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from reclaim.contracts.audit import AuditRow, append_row
from reclaim.spine.tables import audit_log


def _tail_row(conn: Connection) -> AuditRow | None:
    stored = conn.execute(
        sa.select(audit_log.c.data).order_by(audit_log.c.sequence.desc()).limit(1)
    ).scalar_one_or_none()
    if stored is None:
        return None
    # Decoded through model_validate (not codec.decode_model): keeping row_hash lets
    # AuditRow's validator re-verify the stored row rather than silently re-derive it.
    return AuditRow.model_validate(json.loads(stored))


def append(conn: Connection, **fields: Any) -> AuditRow:
    """Append one row, linked to the current tail, and return it."""
    tail = _tail_row(conn)
    row = append_row([tail] if tail is not None else [], **fields)

    payload = json.loads(row.model_dump_json())
    conn.execute(
        audit_log.insert().values(
            sequence=row.sequence,
            ts=payload["ts"],
            case_id=row.case_id,
            actor=payload["actor"],
            event_type=row.event_type,
            idempotency_key=row.idempotency_key,
            prev_hash=row.prev_hash,
            row_hash=row.row_hash,
            data=row.model_dump_json(),
        )
    )
    return row


def tail(conn: Connection) -> AuditRow | None:
    """The last row in the chain, or ``None`` for an empty log."""
    return _tail_row(conn)


def read_all(conn: Connection) -> list[AuditRow]:
    """Every row, in ascending sequence order."""
    rows = conn.execute(
        sa.select(audit_log.c.data).order_by(audit_log.c.sequence.asc())
    ).scalars()
    return [AuditRow.model_validate(json.loads(r)) for r in rows]
