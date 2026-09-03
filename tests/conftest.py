"""Shared fixtures for the Phase 1 spine tests.

The factory fixtures build *valid* frozen contract objects with sensible defaults
and per-call overrides, so a spine test can say what it cares about and stay silent
about the rest. They import only from ``reclaim.contracts`` (which exists), so they
are safe to evaluate at collection time.

The ``engine``/``conn`` fixtures import ``reclaim.spine`` **lazily**, inside the
fixture body -- a spine that does not import yet must fail only the spine tests
that use these fixtures, never the collection of the 251 contract tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reclaim.contracts.actions import ActionEnvelope, ScheduleDebit
from reclaim.contracts.case import RiskCase
from reclaim.contracts.decline_taxonomy import DeclineClass
from reclaim.contracts.enums import (
    Arm,
    CaseState,
    ObligationKind,
    PlanOrigin,
    Rail,
    RiskClass,
    Segment,
)
from reclaim.contracts.money import Money
from reclaim.contracts.obligations import Obligation
from reclaim.contracts.strata import StratumKey

#: A fixed detection instant. Tests that care about time pass their own; the point
#: of a constant is that two factory calls agree without a shared clock.
BASE_TS = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def make_obligation():
    def _make(**over):
        fields = dict(
            obligation_id="obl_1",
            kind=ObligationKind.SUBSCRIPTION_INVOICE,
            payer_id="payer_1",
            gross_amount=Money.from_rupees(1499),
            issued_at=BASE_TS - timedelta(days=30),
            due_at=BASE_TS - timedelta(days=2),
        )
        fields.update(over)
        return Obligation(**fields)

    return _make


#: The decline class the D1 factory defaults to. Deliberately the ambiguous one
#: (§2): it is the class the whole product exists for, so the fixture that flows
#: through the spine carries the case the policy engine finds hardest.
DEFAULT_DECLINE_CLASS = DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS


@pytest.fixture
def make_case():
    """Build a valid ``RiskCase``.

    A D1 case stratifies on its **decline class** by default (JC-42), because that
    is what a real detector produces: the failure it opened the case on is an
    observed PSP code, not the detector's own name for itself. Before JC-42 this
    factory passed ``risk_class`` on every axis, so no test in the suite ever
    constructed a case carrying a ``DeclineClass`` -- which is precisely why a
    validator that made that impossible survived 251 green tests.

    Pass ``canonical_decline_class=None`` for the not-yet-normalised D1 case, or a
    non-D1 ``risk_class`` to get the risk-class stratification.
    """

    def _make(**over):
        amount = over.pop("amount_at_risk", Money.from_rupees(1499))
        segment = over.pop("segment", Segment.B2C_STANDARD)
        risk_class = over.pop("risk_class", RiskClass.FAILED_RECURRING_DEBIT)
        is_d1 = risk_class is RiskClass.FAILED_RECURRING_DEBIT
        decline_class = over.pop(
            "canonical_decline_class", DEFAULT_DECLINE_CLASS if is_d1 else None
        )
        # The stratum axis follows the class when there is one; a D1 case whose
        # code has not been normalised falls back to the risk class (JC-42).
        failure_class = decline_class if decline_class is not None else risk_class
        fields = dict(
            case_id="case_1",
            obligation_id="obl_1",
            payer_id="payer_1",
            risk_class=risk_class,
            segment=segment,
            canonical_decline_class=decline_class,
            amount_at_risk=amount,
            detected_at=BASE_TS,
            stratum=StratumKey.build(
                amount=amount, failure_class=failure_class, segment=segment
            ),
            arm=Arm.A1,
            state=CaseState.DETECTED,
            recovery_window_ends_at=BASE_TS + timedelta(days=14),
        )
        fields.update(over)
        return RiskCase(**fields)

    return _make


@pytest.fixture
def make_debit_envelope():
    def _make(**over):
        case_id = over.pop("case_id", "case_1")
        action_id = over.pop("action_id", "act_1")
        debit_fields = dict(
            obligation_id=over.pop("obligation_id", "obl_1"),
            rail=over.pop("rail", Rail.CARD_ONE_TIME),
            amount=over.pop("amount", Money.from_rupees(1499)),
            execute_at=over.pop("execute_at", BASE_TS + timedelta(days=1)),
            attempt_sequence=over.pop("attempt_sequence", 1),
        )
        return ActionEnvelope(
            action_id=action_id,
            case_id=case_id,
            action=ScheduleDebit(**debit_fields),
            proposed_by=over.pop("proposed_by", PlanOrigin.DETERMINISTIC_ROUTER),
            **over,
        )

    return _make


@pytest.fixture
def engine():
    """A fresh in-memory SQLite engine with the spine schema created."""
    from reclaim.spine.db import create_all, get_engine

    eng = get_engine("sqlite://")
    create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def conn(engine):
    """One begun transaction. Committed at block exit, rolled back on error."""
    with engine.begin() as connection:
        yield connection
