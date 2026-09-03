"""Minimal seeded data generator for the RECLAIM spine.

Populates the ledger via its public API (``ledger.upsert_obligation`` +
``ledger.open_case``), never writing to tables directly.  Uses CARD_ONE_TIME
rail only (simplest — no mandate, no pre-debit notice, no ceiling).

Design decisions:
- Fixed seed (``SEED``): Python ``random.Random`` instance, deterministic.
  The ``Random`` instance is local, not the module-global, so nothing in
  ``reclaim.contracts`` needs to change.
- Uses ``assign_arm`` (independent hashing) from ``experiment.py`` for arm
  assignment.  The experiment spec uses the same ``PLANNED_ARM_WEIGHTS_PERMILLE``.
- Two risk classes only: ``FAILED_RECURRING_DEBIT`` (D1) and
  ``OVERDUE_RECEIVABLE`` (D3).  ``StratumKey`` failure_class comes from
  ``DeclineClass`` for D1 and ``RiskClass`` for D3, and a D1 case also records
  that class in ``RiskCase.canonical_decline_class`` (JC-42).  Until JC-42 the
  generator drew a decline class and then discarded it, stratifying D1 on the
  risk class -- the seeded data therefore had one failure_class value across
  every D1 case, which is a single stratum where §12.1 expects four.
- Skip: hidden payer traits, Stripe adapter, the 27 hand-written seed messages.
- The generator returns the list of cases it created, so tests can assert on
  them.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from sqlalchemy.engine import Connection

from reclaim.contracts.decline_taxonomy import DeclineClass
from reclaim.contracts.case import RiskCase
from reclaim.contracts.enums import (
    Arm,
    CaseState,
    ObligationKind,
    ObligationStatus,
    Rail,
    RiskClass,
    Segment,
)
from reclaim.contracts.experiment import (
    ExperimentSpec,
    PLANNED_ARM_WEIGHTS_PERMILLE,
    assign_arm,
)
from reclaim.contracts.money import Money
from reclaim.contracts.obligations import Obligation
from reclaim.contracts.strata import StratumKey
from reclaim.contracts.temporal import UtcDatetime
from reclaim.spine import ledger

__all__ = ["generate", "make_experiment_spec", "SEED", "CASE_COUNT"]

#: Fixed seed for reproducibility.
SEED = 42

#: Default case count — enough variety without being slow.
CASE_COUNT = 30

#: The detection baseline.  All times are relative to this.
_EPOCH = datetime(2026, 8, 15, 10, 0, 0, tzinfo=timezone.utc)

#: Amount tiers in rupees — span all five amount bands.
_AMOUNT_TIERS_RUPEES: tuple[Decimal, ...] = (
    Decimal("499"),       # le_2k
    Decimal("1499"),      # le_2k
    Decimal("4999"),      # le_15k
    Decimal("14999"),     # le_15k
    Decimal("25000"),     # le_1l
    Decimal("75000"),     # le_1l
    Decimal("150000"),    # le_10l
)

_SEGMENTS: tuple[Segment, ...] = (
    Segment.B2C_STANDARD,
    Segment.B2C_PREMIUM,
    Segment.B2B_SMB,
    Segment.B2B_MID_MARKET,
)

#: Decline classes for D1 (failed-debit) cases — the failure_class axis
#: of the stratum key.
_DECLINE_CLASSES: tuple[DeclineClass, ...] = (
    DeclineClass.INSUFFICIENT_FUNDS,
    DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS,
    DeclineClass.CARD_EXPIRED,
    DeclineClass.ISSUER_TRANSIENT_DECLINE,
)


def make_experiment_spec(
    *,
    experiment_id: str = "exp_seed_dev_001",
    salt: str = "reclaim-seed-salt-v1",
    registered_at: datetime | None = None,
) -> ExperimentSpec:
    """Build a deterministic experiment spec suitable for seeding."""
    return ExperimentSpec(
        experiment_id=experiment_id,
        experiment_salt=salt,
        arm_weights_permille=PLANNED_ARM_WEIGHTS_PERMILLE,
        control_arm=Arm.A1,
        treatment_arm=Arm.A4,
        planned_case_count=CASE_COUNT,
        stopping_rule=(
            "Run all cases to completion of their recovery window; "
            "no early stopping."
        ),
        registered_at=registered_at or _EPOCH - timedelta(days=1),
    )


def generate(
    conn: Connection,
    *,
    n: int = CASE_COUNT,
    seed: int = SEED,
    spec: ExperimentSpec | None = None,
    epoch: datetime | None = None,
) -> list[RiskCase]:
    """Generate ``n`` seeded obligations + risk cases via the spine public API.

    Returns the list of opened ``RiskCase`` objects.

    The generator deterministically alternates between:
    - Failed recurring debits (D1) on CARD_ONE_TIME with varied decline classes
    - Overdue receivables (D3) as B2B invoices

    Every obligation is upserted, then a case is opened on it.  The arm is
    assigned from the experiment spec's salt and the case_id.
    """
    rng = random.Random(seed)
    base = epoch or _EPOCH
    if spec is None:
        spec = make_experiment_spec(registered_at=base - timedelta(days=1))

    cases: list[RiskCase] = []

    for i in range(n):
        idx = i + 1
        payer_id = f"payer_{idx:04d}"
        obl_id = f"obl_{idx:04d}"
        case_id = f"case_{idx:04d}"

        # Alternate: even = failed debit (D1), odd = overdue receivable (D3)
        is_d1 = (i % 3 != 2)  # roughly 2/3 D1, 1/3 D3

        segment = rng.choice(_SEGMENTS)
        amount_rupees = rng.choice(_AMOUNT_TIERS_RUPEES)
        amount = Money.from_rupees(amount_rupees)

        # Jitter detection time by up to 48h
        jitter = timedelta(hours=rng.randint(0, 48), minutes=rng.randint(0, 59))
        detected_at = base + jitter

        # Obligation issued 30 days before, due 2 days before detection
        issued_at = detected_at - timedelta(days=30)
        due_at = detected_at - timedelta(days=rng.randint(1, 10))

        if is_d1:
            risk_class = RiskClass.FAILED_RECURRING_DEBIT
            kind = ObligationKind.SUBSCRIPTION_INVOICE
            decline_class: DeclineClass | None = rng.choice(_DECLINE_CLASSES)
            failure_class = decline_class
        else:
            risk_class = RiskClass.OVERDUE_RECEIVABLE
            kind = ObligationKind.B2B_INVOICE
            decline_class = None
            failure_class = risk_class  # D3 stratifies on risk_class, not decline

        obligation = Obligation(
            obligation_id=obl_id,
            kind=kind,
            payer_id=payer_id,
            gross_amount=amount,
            issued_at=issued_at,
            due_at=due_at,
            status=ObligationStatus.OPEN,
        )
        ledger.upsert_obligation(conn, obligation)

        stratum = StratumKey.build(
            amount=amount,
            failure_class=failure_class,
            segment=segment,
        )

        arm = assign_arm(case_id, spec)

        # Recovery window: 21 days for B2C, 45 for B2B
        window_days = spec.recovery_window_days(segment)
        recovery_window_ends_at = detected_at + timedelta(days=window_days)

        case = RiskCase(
            case_id=case_id,
            obligation_id=obl_id,
            payer_id=payer_id,
            risk_class=risk_class,
            segment=segment,
            canonical_decline_class=decline_class,
            amount_at_risk=amount,
            detected_at=detected_at,
            stratum=stratum,
            arm=arm,
            state=CaseState.DETECTED,
            recovery_window_ends_at=recovery_window_ends_at,
            experiment_id=spec.experiment_id,
        )
        opened = ledger.open_case(conn, case)
        cases.append(opened)

    return cases
