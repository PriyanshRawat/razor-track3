"""The Revenue-at-Risk ledger (Phase 1 spine).

Two tables, one rule. ``obligations`` is the thing owed; ``risk_cases`` is one
recovery attempt against it. §4/§13 say the ledger holds **one row per obligation,
no double counting** -- read here as *one live case per obligation at a time*.

``open_case`` is the only way a case enters the ledger, and it does two writes in the
caller's transaction: the ``risk_cases`` row and its ``case_opened`` audit row. They
commit together, so a case can never exist without the row that explains why it was
opened. The one-live-case rule is guarded twice: a friendly pre-check that raises
``DuplicateActiveCase``, and -- for the concurrent race the pre-check cannot see --
the ``uq_risk_case_active_obligation`` partial UNIQUE index, which turns the second
insert into an ``IntegrityError``.

Rows are stored the same way everywhere in the spine: queryable columns for SQL, and
a lossless ``data`` JSON blob that is the source of truth on read (see ``codec``).
``case_to_row`` is public because a test needs it to exercise the DB constraint
directly, and because it documents exactly which columns shadow which model fields.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from reclaim.contracts.canonical import digest
from reclaim.contracts.case import RiskCase
from reclaim.contracts.enums import ActorType, CaseState
from reclaim.contracts.obligations import Obligation
from reclaim.spine import audit_store
from reclaim.spine.codec import decode_model
from reclaim.spine.errors import DuplicateActiveCase
from reclaim.spine.tables import TERMINAL_STATE_VALUES, obligations, risk_cases

__all__ = [
    "case_to_row",
    "get_case",
    "get_obligation",
    "list_at_risk",
    "list_awaiting_approval",
    "obligation_to_row",
    "open_case",
    "upsert_obligation",
]


# ---------------------------------------------------------------------------
# Row encoders. The JSON in ``data`` is authoritative; the columns shadow it.
# ---------------------------------------------------------------------------


def obligation_to_row(obligation: Obligation) -> dict[str, Any]:
    """Queryable columns for an ``Obligation``. ``created_at`` is set by the writer."""
    payload = json.loads(obligation.model_dump_json())
    return {
        "obligation_id": obligation.obligation_id,
        "payer_id": obligation.payer_id,
        "kind": payload["kind"],
        "currency": payload["currency"],
        "gross_amount_paise": obligation.gross_amount.paise,
        "status": payload["status"],
        "due_at": payload["due_at"],
        "data": obligation.model_dump_json(),
    }


def case_to_row(case: RiskCase) -> dict[str, Any]:
    """Queryable columns for a ``RiskCase``.

    Excludes the bookkeeping ``created_at``/``updated_at`` columns on purpose: those
    belong to the write operation (an insert stamps both; a state change stamps
    ``updated_at``), not to the model.
    """
    payload = json.loads(case.model_dump_json())
    return {
        "case_id": case.case_id,
        "obligation_id": case.obligation_id,
        "payer_id": case.payer_id,
        "arm": payload["arm"],
        "segment": payload["segment"],
        "risk_class": payload["risk_class"],
        "amount_at_risk_paise": case.amount_at_risk.paise,
        "currency": case.amount_at_risk.currency.value,
        "state": payload["state"],
        "stratum_key": case.stratum.key,
        "detected_at": payload["detected_at"],
        "recovery_window_ends_at": payload["recovery_window_ends_at"],
        "stop_reason": payload.get("stop_reason"),
        "stopped_at": payload.get("stopped_at"),
        "data": case.model_dump_json(),
    }


# ---------------------------------------------------------------------------
# Obligations
# ---------------------------------------------------------------------------


def upsert_obligation(conn: Connection, obligation: Obligation) -> None:
    """Insert the obligation, or overwrite it if the id already exists.

    A portable read-then-write rather than a dialect ``ON CONFLICT``: obligations are
    written by a single ingest path, so last-writer-wins is acceptable and the
    ``obligation_id`` PK still forbids duplicate rows. Race-safe idempotency is the
    outbox's job, not this one's.
    """
    row = obligation_to_row(obligation)
    already_there = conn.execute(
        sa.select(obligations.c.obligation_id).where(
            obligations.c.obligation_id == obligation.obligation_id
        )
    ).first()
    if already_there is None:
        issued_at = json.loads(obligation.model_dump_json())["issued_at"]
        conn.execute(obligations.insert().values(**row, created_at=issued_at))
    else:
        conn.execute(
            obligations.update()
            .where(obligations.c.obligation_id == obligation.obligation_id)
            .values(**row)
        )


def get_obligation(conn: Connection, obligation_id: str) -> Obligation | None:
    """Read one obligation back, or ``None`` if unknown."""
    raw = conn.execute(
        sa.select(obligations.c.data).where(
            obligations.c.obligation_id == obligation_id
        )
    ).scalar_one_or_none()
    return decode_model(Obligation, raw) if raw is not None else None


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def open_case(
    conn: Connection,
    case: RiskCase,
    *,
    actor: ActorType = ActorType.SYSTEM,
    at: Any = None,
    rationale: str = "detector opened the case",
) -> RiskCase:
    """Persist a freshly detected case and its ``case_opened`` audit row, atomically.

    A case is opened in ``DETECTED`` -- any other state is a caller bug, so it is a
    ``ValueError``, not a domain error. A second live case on the same obligation is a
    domain error (``DuplicateActiveCase``).
    """
    if case.state is not CaseState.DETECTED:
        raise ValueError(
            f"a case is opened in state 'detected', not {case.state.value!r}; "
            "state changes go through case_machine.transition"
        )

    live = conn.execute(
        sa.select(risk_cases.c.case_id).where(
            risk_cases.c.obligation_id == case.obligation_id,
            risk_cases.c.state.notin_(TERMINAL_STATE_VALUES),
        )
    ).first()
    if live is not None:
        raise DuplicateActiveCase(
            f"obligation {case.obligation_id!r} already has a live case "
            f"({live[0]!r}); the ledger holds one live row per obligation (§4/§13)"
        )

    row = case_to_row(case)
    stamp = row["detected_at"]
    conn.execute(risk_cases.insert().values(**row, created_at=stamp, updated_at=stamp))

    audit_store.append(
        conn,
        ts=at if at is not None else case.detected_at,
        case_id=case.case_id,
        actor=actor,
        event_type="case_opened",
        inputs_digest=digest(
            {
                "case_id": case.case_id,
                "obligation_id": case.obligation_id,
                "amount_at_risk_paise": case.amount_at_risk.paise,
                "arm": case.arm.value,
                "stratum": case.stratum.key,
                "detected_at": row["detected_at"],
            }
        ),
        decision_rationale=rationale,
    )
    return case


def get_case(conn: Connection, case_id: str) -> RiskCase | None:
    """Read one case back from its authoritative JSON, or ``None`` if unknown."""
    raw = conn.execute(
        sa.select(risk_cases.c.data).where(risk_cases.c.case_id == case_id)
    ).scalar_one_or_none()
    return decode_model(RiskCase, raw) if raw is not None else None


def list_at_risk(conn: Connection) -> list[RiskCase]:
    """Every non-terminal case, ordered by id for a stable result."""
    rows = conn.execute(
        sa.select(risk_cases.c.data)
        .where(risk_cases.c.state.notin_(TERMINAL_STATE_VALUES))
        .order_by(risk_cases.c.case_id.asc())
    ).scalars()
    return [decode_model(RiskCase, raw) for raw in rows]


def list_awaiting_approval(conn: Connection) -> list[RiskCase]:
    """Every case parked in ``AWAITING_APPROVAL``, ordered by id.

    The queryable half of §9.1's ``ALLOW_WITH_APPROVAL`` edge: a debit above the
    AFA threshold is not refused, it waits here for a human (§14.2 T2). §18.1's
    approval console (item 14) is a Phase 1 UI over this list; this is the list.
    Same shape as ``list_at_risk`` -- a plain, stable query, no pagination.
    """
    rows = conn.execute(
        sa.select(risk_cases.c.data)
        .where(risk_cases.c.state == CaseState.AWAITING_APPROVAL.value)
        .order_by(risk_cases.c.case_id.asc())
    ).scalars()
    return [decode_model(RiskCase, raw) for raw in rows]
