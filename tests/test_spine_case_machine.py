"""Phase 1 spine: the case state machine.

``transition`` is the only way a case changes state after ``open_case``. It refuses
any edge absent from §9.1's ``ALLOWED_CASE_TRANSITIONS`` (``IllegalTransition``), it
rebuilds the case *through* validation so the frozen invariants fire (a stop with no
reason is rejected, not stored), and it writes the new state and its audit row in one
transaction. The tests walk the spine the task named -- DETECTED -> PLANNED ->
SCHEDULED -> EXECUTING -> RECOVERED -- plus the stop path and the two ways a
transition must fail.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reclaim.contracts.enums import CaseState, StopReason
from reclaim.spine import audit_store, case_machine, ledger
from reclaim.spine.errors import CaseNotFound, IllegalTransition

T0 = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)


def _open(conn, make_obligation, make_case, **over):
    ledger.upsert_obligation(conn, make_obligation())
    return ledger.open_case(conn, make_case(**over))


def test_transition_walks_the_spine_to_recovered(conn, make_obligation, make_case):
    _open(conn, make_obligation, make_case)
    steps = [
        (CaseState.PLANNED, timedelta(hours=1)),
        (CaseState.SCHEDULED, timedelta(hours=2)),
        (CaseState.EXECUTING, timedelta(hours=3)),
        (CaseState.RECOVERED, timedelta(hours=4)),
    ]
    for to_state, delta in steps:
        case_machine.transition(conn, "case_1", to_state, at=T0 + delta)
        assert ledger.get_case(conn, "case_1").state is to_state
    assert ledger.get_case(conn, "case_1").is_terminal is True
    assert ledger.list_at_risk(conn) == []


def test_stop_records_its_reason(conn, make_obligation, make_case):
    _open(conn, make_obligation, make_case)
    stopped = case_machine.transition(
        conn,
        "case_1",
        CaseState.STOPPED,
        event_type="case_stopped",
        at=T0 + timedelta(hours=1),
        stop_reason=StopReason.ALREADY_PAID,
        stopped_at=T0 + timedelta(hours=1),
    )
    assert stopped.state is CaseState.STOPPED
    assert stopped.stop_reason is StopReason.ALREADY_PAID
    assert ledger.get_case(conn, "case_1").stop_reason is StopReason.ALREADY_PAID


def test_a_stop_without_a_reason_is_rejected_and_writes_nothing(
    conn, make_obligation, make_case
):
    _open(conn, make_obligation, make_case)
    with pytest.raises(ValueError):
        case_machine.transition(
            conn, "case_1", CaseState.STOPPED, at=T0 + timedelta(hours=1)
        )
    # The case is untouched and no audit row was appended beyond case_opened.
    assert ledger.get_case(conn, "case_1").state is CaseState.DETECTED
    assert [r.event_type for r in audit_store.read_all(conn)] == ["case_opened"]


def test_an_illegal_transition_is_rejected_and_writes_nothing(
    conn, make_obligation, make_case
):
    _open(conn, make_obligation, make_case)
    with pytest.raises(IllegalTransition):
        case_machine.transition(
            conn, "case_1", CaseState.EXECUTING, at=T0 + timedelta(hours=1)
        )
    assert ledger.get_case(conn, "case_1").state is CaseState.DETECTED
    assert [r.event_type for r in audit_store.read_all(conn)] == ["case_opened"]


def test_transition_on_an_unknown_case_raises(conn):
    with pytest.raises(CaseNotFound):
        case_machine.transition(
            conn, "case_nope", CaseState.PLANNED, at=T0 + timedelta(hours=1)
        )


def test_field_updates_flow_through_the_transition(conn, make_obligation, make_case):
    _open(conn, make_obligation, make_case)
    planned = case_machine.transition(
        conn,
        "case_1",
        CaseState.PLANNED,
        at=T0 + timedelta(hours=1),
        active_plan_id="plan_1",
    )
    assert planned.active_plan_id == "plan_1"
    assert ledger.get_case(conn, "case_1").active_plan_id == "plan_1"


def test_each_transition_appends_one_ordered_audit_row(conn, make_obligation, make_case):
    _open(conn, make_obligation, make_case)
    case_machine.transition(conn, "case_1", CaseState.PLANNED, at=T0 + timedelta(hours=1))
    case_machine.transition(conn, "case_1", CaseState.SCHEDULED, at=T0 + timedelta(hours=2))
    rows = audit_store.read_all(conn)
    assert [r.sequence for r in rows] == [0, 1, 2]
    assert [r.event_type for r in rows] == [
        "case_opened",
        "case_state_changed",
        "case_state_changed",
    ]
    assert all(r.case_id == "case_1" for r in rows)
