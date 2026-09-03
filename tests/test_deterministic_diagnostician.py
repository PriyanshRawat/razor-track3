"""Tests for the deterministic (no-LLM) fallback diagnostician.

The diagnostician turns an already-opened ``RiskCase`` into a valid
``Diagnosis`` by table lookup only: dispatch on ``canonical_decline_class`` when
present (D1), else fall back to ``risk_class`` (D3+). No evidence is gathered, so
the confidence it reports is fixed -- a three-rung ladder: ``KNOWN_CAUSE_
CONFIDENCE`` for a clean single-candidate dispatch, ``CONTESTED_CAUSE_CONFIDENCE``
(below the policy floor, so it tiers up) when the taxonomy files alternatives,
``ABSTENTION_CONFIDENCE`` for ``UNKNOWN``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from reclaim.contracts.case import (
    ABSTENTION_CONFIDENCE_CEILING,
    Diagnosis,
    EvidenceRef,
)
from reclaim.contracts.decline_taxonomy import DECLINE_CLASS_META, DeclineClass
from reclaim.contracts.enums import DiagnosisSource, RiskClass, RootCauseClass
from reclaim.contracts.events import EventType
from reclaim.contracts.policy_format import PolicyThresholds
from reclaim.diagnosis.deterministic import (
    ABSTENTION_CONFIDENCE,
    CONTESTED_CAUSE_CONFIDENCE,
    KNOWN_CAUSE_CONFIDENCE,
    diagnose,
)

_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

#: The policy engine's actionable-confidence floor (default). Re-derived here
#: rather than imported so the test tracks the real config default, not a copy.
_DIAGNOSIS_CONFIDENCE_FLOOR = Decimal(
    PolicyThresholds.model_fields["diagnosis_confidence_floor"].default
)


# --- local case builders (thin wrappers over the shared make_case fixture) --

_make_case_fixture = None


@pytest.fixture(autouse=True)
def _bind_make_case(make_case):
    global _make_case_fixture
    _make_case_fixture = make_case
    yield
    _make_case_fixture = None


def make_case_d1(decline_class):
    return _make_case_fixture(
        risk_class=RiskClass.FAILED_RECURRING_DEBIT,
        canonical_decline_class=decline_class,
    )


def make_case_ambiguous():
    return make_case_d1(DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS)


def make_case_nond1(risk_class):
    return _make_case_fixture(risk_class=risk_class)


# --- D1: dispatch on the normalised decline class -------------------------

@pytest.mark.parametrize(
    "decline_class, expected_root_cause",
    [
        (DeclineClass.INSUFFICIENT_FUNDS, RootCauseClass.H1_TIMING_LIQUIDITY),
        (DeclineClass.CARD_EXPIRED, RootCauseClass.H2_CREDENTIAL_LIFECYCLE),
        (DeclineClass.MANDATE_CANCELLED, RootCauseClass.H3_MANDATE_DEAD_OR_PAUSED),
        (
            DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS,
            RootCauseClass.H4_AFA_STEP_UP_INCOMPLETE,
        ),
    ],
)
def test_d1_dispatches_decline_class_to_its_fixed_hypothesis(
    decline_class, expected_root_cause
):
    case = make_case_d1(decline_class)
    dx = diagnose(case, created_at=_NOW)
    assert dx.root_cause is expected_root_cause
    assert dx.source is DiagnosisSource.DETERMINISTIC_FALLBACK


def test_ambiguous_class_picks_h4_default_and_files_h3_h5_as_alternatives():
    """The deterministic path does not disambiguate PAYER_AUTHORIZATION_MISSING_
    AMBIGUOUS. It takes the lowest-blast-radius candidate (H4), records the other
    two, and reports at the contested (tier-up) confidence so a human confirms.
    Choosing between them from evidence is what the LLM adds later.
    """
    case = make_case_ambiguous()
    dx = diagnose(case, created_at=_NOW)
    assert dx.root_cause is RootCauseClass.H4_AFA_STEP_UP_INCOMPLETE
    assert set(dx.alternative_root_causes) == {
        RootCauseClass.H3_MANDATE_DEAD_OR_PAUSED,
        RootCauseClass.H5_DELIBERATE_CHURN_INTENT,
    }
    assert dx.confidence == CONTESTED_CAUSE_CONFIDENCE
    assert dx.confidence < _DIAGNOSIS_CONFIDENCE_FLOOR  # always tiers up


def test_our_side_decline_class_abstains_rather_than_asserting_h6():
    """An our-side systemic root cause (H6) needs an incident or cohort to point
    at (the Diagnosis schema enforces that), and a table lookup has neither. A
    wrong systemic-suppression call is the most damaging false diagnosis, so the
    deterministic path abstains instead.
    """
    case = make_case_d1(DeclineClass.PROCESSING_ERROR)
    dx = diagnose(case, created_at=_NOW)
    assert dx.root_cause is RootCauseClass.UNKNOWN
    assert dx.incident_id is None and dx.cohort_id is None


def test_unmapped_decline_class_abstains():
    case = make_case_d1(DeclineClass.UNKNOWN_UNMAPPED)
    dx = diagnose(case, created_at=_NOW)
    assert dx.root_cause is RootCauseClass.UNKNOWN


def test_d1_without_a_normalised_decline_class_abstains():
    case = make_case_d1(None)
    assert case.canonical_decline_class is None
    dx = diagnose(case, created_at=_NOW)
    assert dx.root_cause is RootCauseClass.UNKNOWN


# --- D3+: fall back to risk_class ----------------------------------------

def test_overdue_receivable_falls_back_to_h9_and_tiers_up():
    case = make_case_nond1(RiskClass.OVERDUE_RECEIVABLE)
    dx = diagnose(case, created_at=_NOW)
    assert dx.root_cause is RootCauseClass.H9_B2B_LIQUIDITY_OR_WILLFUL_DELAY
    assert RootCauseClass.H8_B2B_PROCESS_DEFECT in dx.alternative_root_causes
    # H8 vs H9 are different actions and the table cannot tell them apart, so a
    # bare D3 diagnosis is contested: it tiers up rather than auto-escalating.
    assert dx.confidence == CONTESTED_CAUSE_CONFIDENCE
    assert dx.confidence < _DIAGNOSIS_CONFIDENCE_FLOOR


@pytest.mark.parametrize(
    "risk_class",
    [
        RiskClass.PREDICTED_TO_FAIL_DEBIT,
        RiskClass.CHECKOUT_ABANDONMENT,
        RiskClass.SYSTEMIC_AUTH_DEGRADATION,
        RiskClass.SILENT_LEAKAGE,
    ],
)
def test_other_risk_classes_have_no_table_default_and_abstain(risk_class):
    case = make_case_nond1(risk_class)
    dx = diagnose(case, created_at=_NOW)
    assert dx.root_cause is RootCauseClass.UNKNOWN


# --- the confidence ladder --------------------------------------------

def test_confidence_ladder_is_ordered_and_the_contested_rung_tiers_up():
    assert ABSTENTION_CONFIDENCE <= ABSTENTION_CONFIDENCE_CEILING
    assert ABSTENTION_CONFIDENCE_CEILING < CONTESTED_CAUSE_CONFIDENCE
    # the load-bearing one: a contested diagnosis is always below the policy
    # floor, so it can never auto-act on a coin flip between opposite actions.
    assert CONTESTED_CAUSE_CONFIDENCE < _DIAGNOSIS_CONFIDENCE_FLOOR
    assert CONTESTED_CAUSE_CONFIDENCE < KNOWN_CAUSE_CONFIDENCE
    assert KNOWN_CAUSE_CONFIDENCE > ABSTENTION_CONFIDENCE_CEILING
    # a clean known cause must not read as certain either
    assert KNOWN_CAUSE_CONFIDENCE <= ABSTENTION_CONFIDENCE_CEILING + Decimal("0.2")


def test_contested_rung_is_driven_by_alternatives_not_a_hardcoded_class():
    """The lower, tier-up confidence attaches to *any* dispatch that files
    alternative_root_causes; a clean single-cause dispatch keeps
    KNOWN_CAUSE_CONFIDENCE. Nothing here is keyed to a class name."""
    for decline_class in DeclineClass:
        dx = diagnose(make_case_d1(decline_class), created_at=_NOW)
        if dx.root_cause is RootCauseClass.UNKNOWN:
            continue
        if dx.alternative_root_causes:
            assert dx.confidence == CONTESTED_CAUSE_CONFIDENCE
            assert dx.confidence < _DIAGNOSIS_CONFIDENCE_FLOOR
        else:
            assert dx.confidence == KNOWN_CAUSE_CONFIDENCE


@pytest.mark.parametrize(
    "decline_class",
    [
        DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS,  # H4 / H3 / H5
        DeclineClass.MANDATE_PAUSED,                          # H3 / H5
    ],
)
def test_taxonomy_contested_classes_tier_up_below_the_floor(decline_class):
    dx = diagnose(make_case_d1(decline_class), created_at=_NOW)
    assert dx.root_cause is not RootCauseClass.UNKNOWN
    assert dx.alternative_root_causes  # taxonomy files > 1 candidate
    assert dx.confidence == CONTESTED_CAUSE_CONFIDENCE
    assert dx.confidence < _DIAGNOSIS_CONFIDENCE_FLOOR


@pytest.mark.parametrize(
    "decline_class",
    [
        DeclineClass.INSUFFICIENT_FUNDS,
        DeclineClass.CARD_EXPIRED,
        DeclineClass.MANDATE_CANCELLED,
        DeclineClass.AUTHENTICATION_REQUIRED,
    ],
)
def test_clean_single_cause_classes_keep_known_cause_confidence(decline_class):
    dx = diagnose(make_case_d1(decline_class), created_at=_NOW)
    assert dx.alternative_root_causes == ()
    assert dx.confidence == KNOWN_CAUSE_CONFIDENCE
    assert dx.is_abstention is False


def test_abstention_confidence_stays_under_the_ceiling_and_parses():
    dx = diagnose(make_case_d1(DeclineClass.UNKNOWN_UNMAPPED), created_at=_NOW)
    assert dx.confidence == ABSTENTION_CONFIDENCE
    assert dx.confidence <= ABSTENTION_CONFIDENCE_CEILING
    assert dx.is_abstention is True


# --- every path yields a schema-valid Diagnosis ------------------------

def test_every_decline_class_yields_a_valid_diagnosis_and_never_asserts_h6():
    for decline_class in DeclineClass:
        case = make_case_d1(decline_class)
        dx = diagnose(case, created_at=_NOW)
        assert isinstance(dx, Diagnosis)
        assert dx.case_id == case.case_id
        assert dx.source is DiagnosisSource.DETERMINISTIC_FALLBACK
        assert dx.root_cause is not RootCauseClass.H6_OUR_SIDE_SYSTEMIC
        if dx.root_cause is RootCauseClass.UNKNOWN:
            assert dx.confidence == ABSTENTION_CONFIDENCE
            assert dx.confidence <= ABSTENTION_CONFIDENCE_CEILING
        else:
            # known cause -> one of exactly two rungs, per the alternatives shape
            expected = (
                CONTESTED_CAUSE_CONFIDENCE
                if dx.alternative_root_causes
                else KNOWN_CAUSE_CONFIDENCE
            )
            assert dx.confidence == expected
            assert dx.confidence > ABSTENTION_CONFIDENCE_CEILING
            # the chosen cause is the taxonomy primary candidate for the class
            assert (
                dx.root_cause
                is DECLINE_CLASS_META[decline_class].candidate_root_causes[0]
            )


def test_every_risk_class_yields_a_valid_diagnosis():
    for risk_class in RiskClass:
        if risk_class is RiskClass.FAILED_RECURRING_DEBIT:
            case = make_case_d1(None)  # un-normalised D1 exercises the fallback branch
        else:
            case = make_case_nond1(risk_class)
        dx = diagnose(case, created_at=_NOW)
        assert isinstance(dx, Diagnosis)
        assert dx.source is DiagnosisSource.DETERMINISTIC_FALLBACK


# --- evidence / claims -------------------------------------------------

def test_diagnosis_synthesises_one_cited_claim_from_the_case_when_none_supplied():
    case = make_case_d1(DeclineClass.CARD_EXPIRED)
    dx = diagnose(case, created_at=_NOW)
    assert len(dx.claims) == 1
    (claim,) = dx.claims
    assert len(claim.evidence) == 1
    (ref,) = claim.evidence
    assert ref.source_event_id == case.case_id
    assert ref.observed_at == case.detected_at
    assert ref.event_type is EventType.PAYMENT_ATTEMPT  # D1


def test_caller_supplied_evidence_is_used_verbatim():
    case = make_case_d1(DeclineClass.INSUFFICIENT_FUNDS)
    ref = EvidenceRef(
        evidence_id="ev_real_1",
        source_system="stripe_test",
        source_event_id="evt_abc123",
        event_type=EventType.PAYMENT_ATTEMPT,
        observed_at=_NOW,
        summary="charge failed: insufficient_funds",
    )
    dx = diagnose(case, created_at=_NOW, evidence=[ref])
    assert dx.claims[0].evidence == (ref,)


def test_deterministic_diagnosis_omits_the_llm_only_fields():
    dx = diagnose(make_case_d1(DeclineClass.MANDATE_CANCELLED), created_at=_NOW)
    assert dx.model_id is None
    assert dx.prompt_version is None


def test_diagnosis_id_is_derived_deterministically_and_well_formed():
    case = make_case_d1(DeclineClass.CARD_EXPIRED)
    a = diagnose(case, created_at=_NOW)
    b = diagnose(case, created_at=_NOW)
    assert a.diagnosis_id == b.diagnosis_id
    assert a.diagnosis_id.startswith("dx_")


def test_explicit_diagnosis_id_is_honoured():
    case = make_case_d1(DeclineClass.CARD_EXPIRED)
    dx = diagnose(case, created_at=_NOW, diagnosis_id="dx_custom_1")
    assert dx.diagnosis_id == "dx_custom_1"
