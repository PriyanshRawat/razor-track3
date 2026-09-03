"""Phase 1 spine: the Revenue-at-Risk ledger.

Opening a case writes the ledger row and its ``case_opened`` audit row in one
transaction, so a case can never exist unaudited. The ledger holds at most one *live*
case per obligation -- "one row per obligation, no double counting" (§4/§13) -- which
is guarded two ways: a friendly pre-check, and a partial UNIQUE index that also holds
against a concurrent race. The last test proves the index itself, not just the
pre-check, because a table is frozen only when a test walks the constraint.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from reclaim.contracts.enums import CaseState, ObligationStatus, StopReason
from reclaim.spine import audit_store, ledger
from reclaim.spine.errors import DuplicateActiveCase
from reclaim.spine.tables import obligations, risk_cases


def test_open_case_persists_and_reads_back(conn, make_obligation, make_case):
    ledger.upsert_obligation(conn, make_obligation())
    case = make_case()
    ledger.open_case(conn, case)
    assert ledger.get_case(conn, case.case_id) == case


def test_open_case_writes_a_case_opened_audit_row(conn, make_obligation, make_case):
    ledger.upsert_obligation(conn, make_obligation())
    case = make_case()
    ledger.open_case(conn, case)
    rows = audit_store.read_all(conn)
    assert [(r.event_type, r.case_id) for r in rows] == [("case_opened", case.case_id)]


def test_get_case_returns_none_for_an_unknown_id(conn):
    assert ledger.get_case(conn, "case_nope") is None


def test_upsert_obligation_is_idempotent_and_last_write_wins(conn, make_obligation):
    ledger.upsert_obligation(conn, make_obligation())
    ledger.upsert_obligation(conn, make_obligation(status=ObligationStatus.PAID))
    count = conn.execute(
        sa.select(sa.func.count()).select_from(obligations)
    ).scalar_one()
    assert count == 1
    assert ledger.get_obligation(conn, "obl_1").status is ObligationStatus.PAID


def test_open_case_requires_the_detected_state(conn, make_obligation, make_case):
    ledger.upsert_obligation(conn, make_obligation())
    planned = make_case(state=CaseState.PLANNED)
    with pytest.raises(ValueError):
        ledger.open_case(conn, planned)


def test_open_case_rejects_a_second_live_case_on_one_obligation(
    conn, make_obligation, make_case
):
    ledger.upsert_obligation(conn, make_obligation())
    ledger.open_case(conn, make_case(case_id="case_1"))
    with pytest.raises(DuplicateActiveCase):
        ledger.open_case(conn, make_case(case_id="case_2"))


def test_list_at_risk_returns_the_live_cases(conn, make_obligation, make_case):
    ledger.upsert_obligation(conn, make_obligation(obligation_id="obl_1"))
    ledger.upsert_obligation(conn, make_obligation(obligation_id="obl_2"))
    ledger.open_case(conn, make_case(case_id="case_1", obligation_id="obl_1"))
    ledger.open_case(conn, make_case(case_id="case_2", obligation_id="obl_2"))
    assert {c.case_id for c in ledger.list_at_risk(conn)} == {"case_1", "case_2"}


def test_list_awaiting_approval_returns_only_the_parked_cases(
    conn, make_obligation, make_case
):
    """The queryable half of §9.1's ALLOW_WITH_APPROVAL edge. A parked case is
    still non-terminal, so it also remains in ``list_at_risk``."""
    from reclaim.spine import case_machine

    ledger.upsert_obligation(conn, make_obligation(obligation_id="obl_1"))
    ledger.upsert_obligation(conn, make_obligation(obligation_id="obl_2"))
    ledger.open_case(conn, make_case(case_id="case_live", obligation_id="obl_1"))
    ledger.open_case(conn, make_case(case_id="case_wait", obligation_id="obl_2"))

    case_machine.transition(conn, "case_wait", CaseState.PLANNED)
    case_machine.transition(conn, "case_wait", CaseState.AWAITING_APPROVAL)

    assert [c.case_id for c in ledger.list_awaiting_approval(conn)] == ["case_wait"]
    assert {c.case_id for c in ledger.list_at_risk(conn)} == {
        "case_live",
        "case_wait",
    }


def test_the_active_case_unique_index_is_enforced_by_the_database(
    engine, make_obligation, make_case
):
    """Bypass the pre-check and hit the constraint directly: two live rows for one
    obligation must be rejected by the partial UNIQUE index."""
    with engine.begin() as c:
        ledger.upsert_obligation(c, make_obligation())
        ledger.open_case(c, make_case(case_id="case_1"))

    second = make_case(case_id="case_2")  # same obligation, still DETECTED (live)
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(
                risk_cases.insert().values(
                    **ledger.case_to_row(second),
                    created_at="2026-08-01T09:00:00.000000Z",
                    updated_at="2026-08-01T09:00:00.000000Z",
                )
            )


def test_a_terminal_case_frees_the_obligation_for_reopening(
    engine, make_obligation, make_case
):
    """The index is partial: a stopped case does not block a fresh one."""
    with engine.begin() as c:
        ledger.upsert_obligation(c, make_obligation())
        ledger.open_case(c, make_case(case_id="case_1"))
        # Direct terminal row for a *different* case shares the obligation legally,
        # because a stopped case is outside the partial index predicate.
        stopped = make_case(
            case_id="case_1",
            state=CaseState.STOPPED,
            stop_reason=StopReason.ALREADY_PAID,
            stopped_at=make_case().detected_at,
        )
        c.execute(
            risk_cases.update()
            .where(risk_cases.c.case_id == "case_1")
            .values(**ledger.case_to_row(stopped))
        )
        # With case_1 now terminal, opening a new live case on the same obligation
        # is allowed.
        ledger.open_case(c, make_case(case_id="case_2"))
        live = {row.case_id for row in ledger.list_at_risk(c)}
    assert live == {"case_2"}
