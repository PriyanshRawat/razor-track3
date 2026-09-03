"""The case state machine (Phase 1 spine).

``transition`` is the single writer of a case's state after it is opened. Three
properties, checked in this order so that a rejected transition writes nothing:

1. The case exists (``CaseNotFound``).
2. The edge is in §9.1's ``ALLOWED_CASE_TRANSITIONS`` (``IllegalTransition``). This is
   runtime invariant #9: an out-of-table jump is refused, not recorded.
3. The resulting case is *valid*. The new state and any field updates are rebuilt
   **through** ``RiskCase`` validation, so the frozen invariants fire -- a stop with
   no reason, a plan on the control arm, a stopped_at before detection all raise
   ``ValueError`` here rather than being stored as a bad row.

Only then are the ledger row and the audit row written, in the caller's transaction,
so the state change and the row explaining it commit together.

Why rebuild instead of ``model_copy(update=...)``: in Pydantic v2 ``model_copy`` does
**not** re-run validators, so it would happily produce a ``STOPPED`` case with no
``stop_reason``. Re-validating the dumped dict (minus its computed fields) is what
makes the invariants load-bearing on every transition.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.engine import Connection

from reclaim.contracts.canonical import digest
from reclaim.contracts.case import RiskCase
from reclaim.contracts.enums import ActorType, CaseState
from reclaim.contracts.temporal import to_rfc3339, utcnow
from reclaim.spine import audit_store, ledger
from reclaim.spine.errors import CaseNotFound, IllegalTransition
from reclaim.spine.tables import risk_cases

__all__ = ["transition"]


def transition(
    conn: Connection,
    case_id: str,
    to_state: CaseState,
    *,
    actor: ActorType = ActorType.SYSTEM,
    event_type: str = "case_state_changed",
    rationale: str | None = None,
    at: Any = None,
    **field_updates: Any,
) -> RiskCase:
    """Move ``case_id`` to ``to_state``, applying ``field_updates``, atomically.

    ``field_updates`` are extra ``RiskCase`` fields set in the same step -- e.g.
    ``stop_reason`` and ``stopped_at`` when stopping, or ``active_plan_id`` when
    planning. They are validated with the rest of the model.
    """
    current = ledger.get_case(conn, case_id)
    if current is None:
        raise CaseNotFound(f"no case {case_id!r} in the ledger")

    if not current.can_transition_to(to_state):
        raise IllegalTransition(
            f"{current.state.value} -> {to_state.value} is not an allowed transition "
            f"for case {case_id!r} (§9.1)"
        )

    new_case = _rebuild(current, to_state, field_updates)

    when = at if at is not None else utcnow()
    row = ledger.case_to_row(new_case)
    conn.execute(
        risk_cases.update()
        .where(risk_cases.c.case_id == case_id)
        .values(**row, updated_at=to_rfc3339(when))
    )

    audit_store.append(
        conn,
        ts=when,
        case_id=case_id,
        actor=actor,
        event_type=event_type,
        inputs_digest=digest(
            {
                "case_id": case_id,
                "from_state": current.state.value,
                "to_state": to_state.value,
                "field_updates": sorted(field_updates.keys()),
            }
        ),
        decision_rationale=(
            rationale
            if rationale is not None
            else f"{current.state.value} -> {to_state.value}"
        ),
    )
    return new_case


def _rebuild(
    current: RiskCase, to_state: CaseState, field_updates: dict[str, Any]
) -> RiskCase:
    """Re-validate ``current`` with a new state and field updates.

    Computed fields are stripped before validation (they are not settable inputs);
    ``field_updates`` carry raw Python values (enums, datetimes), which Pydantic
    coerces exactly as it would on first construction.
    """
    data = json.loads(current.model_dump_json())
    for computed_name in RiskCase.model_computed_fields:
        data.pop(computed_name, None)
    data["state"] = to_state.value
    data.update(field_updates)
    return RiskCase.model_validate(data)
