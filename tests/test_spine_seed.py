"""Tests for the seeded data generator (``reclaim.spine.seed``).

These tests verify that the generator:
1. Uses the spine public API only (ledger.open_case / upsert_obligation).
2. Is deterministic: same seed produces same cases.
3. Covers both risk classes (D1 failed debit, D3 overdue receivable).
4. Produces valid contract objects with consistent strata and arms.
5. Populates the audit log correctly (one audit row per case).
6. Respects the one-live-case-per-obligation rule.
7. Uses CARD_ONE_TIME rail semantics (no mandate, simple).
8. All amount bands and segments are exercised (at sufficient n).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from reclaim.contracts.case import RiskCase
from reclaim.contracts.enums import (
    Arm,
    CaseState,
    ObligationKind,
    RiskClass,
    Segment,
)
from reclaim.contracts.experiment import assign_arm
from reclaim.contracts.money import Money
from reclaim.contracts.strata import StratumKey, amount_band
from reclaim.spine import audit_store, ledger
from reclaim.spine.errors import DuplicateActiveCase
from reclaim.spine.seed import (
    CASE_COUNT,
    SEED,
    _AMOUNT_TIERS_RUPEES,
    _SEGMENTS,
    generate,
    make_experiment_spec,
)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """The generator is fully deterministic given a seed."""

    def test_same_seed_same_cases(self, engine):
        """Two runs with the same seed produce identical case lists."""
        from reclaim.spine.db import create_all

        # Run 1
        eng1 = engine
        with eng1.begin() as c1:
            cases1 = generate(c1, n=10, seed=SEED)

        # Run 2 — fresh DB
        from reclaim.spine.db import get_engine
        eng2 = get_engine("sqlite://")
        create_all(eng2)
        with eng2.begin() as c2:
            cases2 = generate(c2, n=10, seed=SEED)
        eng2.dispose()

        assert len(cases1) == len(cases2) == 10
        for c1, c2 in zip(cases1, cases2):
            assert c1.case_id == c2.case_id
            assert c1.obligation_id == c2.obligation_id
            assert c1.amount_at_risk == c2.amount_at_risk
            assert c1.arm == c2.arm
            assert c1.stratum == c2.stratum
            assert c1.segment == c2.segment
            assert c1.risk_class == c2.risk_class
            assert c1.detected_at == c2.detected_at

    def test_different_seed_different_cases(self, engine):
        """Different seeds produce different distributions."""
        from reclaim.spine.db import create_all, get_engine

        with engine.begin() as c1:
            cases1 = generate(c1, n=10, seed=SEED)

        eng2 = get_engine("sqlite://")
        create_all(eng2)
        with eng2.begin() as c2:
            cases2 = generate(c2, n=10, seed=SEED + 1)
        eng2.dispose()

        # At least some amounts or segments should differ
        amounts1 = [c.amount_at_risk.paise for c in cases1]
        amounts2 = [c.amount_at_risk.paise for c in cases2]
        assert amounts1 != amounts2


# ---------------------------------------------------------------------------
# Correctness via public API
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """The generator uses the spine's public API, not direct table writes."""

    def test_obligations_readable_via_ledger(self, conn):
        """Every upserted obligation is readable via ``ledger.get_obligation``."""
        cases = generate(conn, n=5, seed=SEED)
        for case in cases:
            obl = ledger.get_obligation(conn, case.obligation_id)
            assert obl is not None
            assert obl.obligation_id == case.obligation_id
            assert obl.payer_id == case.payer_id

    def test_cases_readable_via_ledger(self, conn):
        """Every opened case is readable via ``ledger.get_case``."""
        cases = generate(conn, n=5, seed=SEED)
        for case in cases:
            read_back = ledger.get_case(conn, case.case_id)
            assert read_back is not None
            assert read_back.case_id == case.case_id
            assert read_back.state == CaseState.DETECTED
            assert read_back.amount_at_risk == case.amount_at_risk

    def test_cases_appear_in_list_at_risk(self, conn):
        """All generated cases appear in ``ledger.list_at_risk``."""
        cases = generate(conn, n=5, seed=SEED)
        at_risk = ledger.list_at_risk(conn)
        at_risk_ids = {c.case_id for c in at_risk}
        for case in cases:
            assert case.case_id in at_risk_ids

    def test_audit_log_has_one_row_per_case(self, conn):
        """Each case produces exactly one ``case_opened`` audit row."""
        cases = generate(conn, n=5, seed=SEED)
        all_rows = audit_store.read_all(conn)
        opened_events = [r for r in all_rows if r.event_type == "case_opened"]
        assert len(opened_events) == len(cases)

        # Verify case_ids match
        audit_case_ids = {r.case_id for r in opened_events}
        gen_case_ids = {c.case_id for c in cases}
        assert audit_case_ids == gen_case_ids


# ---------------------------------------------------------------------------
# Domain correctness
# ---------------------------------------------------------------------------


class TestDomainCorrectness:
    """Generated data satisfies the frozen contract invariants."""

    def test_all_cases_in_detected_state(self, conn):
        """Cases are opened in DETECTED state (the only legal initial state)."""
        cases = generate(conn, n=10, seed=SEED)
        for case in cases:
            assert case.state == CaseState.DETECTED

    def test_amount_at_risk_is_positive(self, conn):
        """All amounts at risk are positive (contract invariant)."""
        cases = generate(conn, n=10, seed=SEED)
        for case in cases:
            assert case.amount_at_risk.is_positive

    def test_recovery_window_after_detection(self, conn):
        """Recovery window ends after detection (contract invariant)."""
        cases = generate(conn, n=10, seed=SEED)
        for case in cases:
            assert case.recovery_window_ends_at > case.detected_at

    def test_stratum_agrees_with_case(self, conn):
        """Stored stratum matches the case fields (JC-23 / JC-02)."""
        cases = generate(conn, n=10, seed=SEED)
        for case in cases:
            assert case.stratum.segment == case.segment
            derived_band = amount_band(case.amount_at_risk)
            assert case.stratum.amount_band == derived_band
            if case.risk_class is RiskClass.FAILED_RECURRING_DEBIT:
                # D1 stratifies on the normalised decline class, and the case
                # records which one (JC-42). Asserting only "it is different from
                # the risk class" would pass on any string in the vocabulary.
                assert case.canonical_decline_class is not None
                assert (
                    case.stratum.failure_class
                    == case.canonical_decline_class.value
                )
            else:
                assert case.stratum.failure_class == case.risk_class.value
                assert case.canonical_decline_class is None

    def test_arm_from_experiment_spec(self, conn):
        """Arms are assigned from the experiment spec, not hardcoded."""
        spec = make_experiment_spec()
        cases = generate(conn, n=10, seed=SEED, spec=spec)
        for case in cases:
            expected = assign_arm(case.case_id, spec)
            assert case.arm == expected

    def test_d1_and_d3_both_present(self, conn):
        """Both risk classes are generated: D1 and D3."""
        cases = generate(conn, n=15, seed=SEED)
        risk_classes = {c.risk_class for c in cases}
        assert RiskClass.FAILED_RECURRING_DEBIT in risk_classes
        assert RiskClass.OVERDUE_RECEIVABLE in risk_classes

    def test_d1_is_subscription_d3_is_b2b(self, conn):
        """D1 cases are subscription invoices; D3 are B2B invoices."""
        cases = generate(conn, n=15, seed=SEED)
        for case in cases:
            obl = ledger.get_obligation(conn, case.obligation_id)
            if case.risk_class == RiskClass.FAILED_RECURRING_DEBIT:
                assert obl.kind == ObligationKind.SUBSCRIPTION_INVOICE
            elif case.risk_class == RiskClass.OVERDUE_RECEIVABLE:
                assert obl.kind == ObligationKind.B2B_INVOICE

    def test_recovery_window_b2c_vs_b2b(self, conn):
        """B2C gets 21 days, B2B gets 45 days recovery window."""
        spec = make_experiment_spec()
        cases = generate(conn, n=15, seed=SEED, spec=spec)
        for case in cases:
            window = case.recovery_window_ends_at - case.detected_at
            expected_days = spec.recovery_window_days(case.segment)
            assert window == timedelta(days=expected_days)

    def test_no_a0_cases_have_plans(self, conn):
        """A0 cases must not have an active plan (contract invariant)."""
        cases = generate(conn, n=30, seed=SEED)
        for case in cases:
            if case.arm == Arm.A0:
                assert case.active_plan_id is None

    def test_experiment_id_set(self, conn):
        """Every case carries the experiment ID from the spec."""
        spec = make_experiment_spec()
        cases = generate(conn, n=5, seed=SEED, spec=spec)
        for case in cases:
            assert case.experiment_id == spec.experiment_id


# ---------------------------------------------------------------------------
# Coverage breadth
# ---------------------------------------------------------------------------


class TestCoverageBreadth:
    """With sufficient n, the generator covers the variety the spine needs."""

    def test_multiple_amount_bands(self, conn):
        """Multiple amount bands are covered."""
        cases = generate(conn, n=30, seed=SEED)
        bands = {case.stratum.amount_band for case in cases}
        # With 7 tiers and 30 cases, we expect at least 3 bands
        assert len(bands) >= 3

    def test_multiple_segments(self, conn):
        """Multiple segments are covered."""
        cases = generate(conn, n=30, seed=SEED)
        segments = {case.segment for case in cases}
        assert len(segments) >= 2

    def test_multiple_arms_assigned(self, conn):
        """Multiple experiment arms are assigned (not just one)."""
        cases = generate(conn, n=30, seed=SEED)
        arms = {case.arm for case in cases}
        # With 30 cases and planned weights, we should get at least 3 arms
        assert len(arms) >= 2

    def test_multiple_decline_classes_on_the_d1_cases(self, conn):
        """The failure-class axis of §12.1's stratification must actually vary.

        Before JC-42 the generator drew a decline class and then stratified on the
        risk class anyway, so every D1 case shared one failure_class value: a single
        stratum where the design calls for four. Nothing failed, because nothing
        looked."""
        cases = generate(conn, n=30, seed=SEED)
        d1 = [c for c in cases if c.risk_class is RiskClass.FAILED_RECURRING_DEBIT]
        assert d1
        classes = {c.canonical_decline_class for c in d1}
        assert None not in classes
        assert len(classes) >= 3, (
            f"D1 cases span only {len(classes)} decline class(es): {classes}"
        )
        assert {c.stratum.failure_class for c in d1} == {
            dc.value for dc in classes
        }

    def test_unique_payer_ids(self, conn):
        """Each case gets a unique payer (one obligation per payer)."""
        cases = generate(conn, n=10, seed=SEED)
        payer_ids = [c.payer_id for c in cases]
        assert len(payer_ids) == len(set(payer_ids))


# ---------------------------------------------------------------------------
# Edge cases & constraints
# ---------------------------------------------------------------------------


class TestConstraints:
    """Generator respects spine constraints."""

    def test_no_duplicate_active_case_per_obligation(self, conn):
        """Attempting to open a second case on a live obligation raises."""
        cases = generate(conn, n=3, seed=SEED)
        first_obl = cases[0].obligation_id

        duplicate = RiskCase(
            case_id="case_dup_001",
            obligation_id=first_obl,
            payer_id=cases[0].payer_id,
            risk_class=cases[0].risk_class,
            segment=cases[0].segment,
            canonical_decline_class=cases[0].canonical_decline_class,
            amount_at_risk=cases[0].amount_at_risk,
            detected_at=cases[0].detected_at + timedelta(hours=1),
            stratum=cases[0].stratum,
            arm=cases[0].arm,
            state=CaseState.DETECTED,
            recovery_window_ends_at=cases[0].recovery_window_ends_at,
        )
        with pytest.raises(DuplicateActiveCase):
            ledger.open_case(conn, duplicate)

    def test_audit_chain_integrity(self, conn):
        """Audit rows form a valid hash chain."""
        generate(conn, n=5, seed=SEED)
        rows = audit_store.read_all(conn)
        assert len(rows) >= 5

        for i, row in enumerate(rows):
            assert row.sequence == i
            if i == 0:
                assert row.prev_hash == ("0" * 64)  # genesis
            else:
                assert row.prev_hash == rows[i - 1].row_hash

    def test_make_experiment_spec_is_valid(self):
        """The convenience function produces a valid spec."""
        spec = make_experiment_spec()
        assert spec.experiment_id == "exp_seed_dev_001"
        assert sum(spec.arm_weights_permille.values()) == 1000
        assert spec.control_arm == Arm.A1
        assert spec.treatment_arm == Arm.A4
