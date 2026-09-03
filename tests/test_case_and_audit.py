"""Contract tests for the case, diagnosis, plan and audit schemas (deliverable #5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from reclaim.contracts.actions import ActionEnvelope, EscalateToHuman, ScheduleDebit
from reclaim.contracts.audit import AuditRow, GENESIS_HASH, append_row, verify_chain
from reclaim.contracts.case import (
    Claim,
    Diagnosis,
    EvidenceRef,
    Plan,
    PlanStep,
    RiskCase,
)
from reclaim.contracts.enums import (
    ALLOWED_CASE_TRANSITIONS,
    ActorType,
    Arm,
    AutonomyTier,
    CaseState,
    DiagnosisSource,
    HumanQueue,
    PlanOrigin,
    Rail,
    RiskClass,
    RootCauseClass,
    Segment,
    StepTrigger,
    StopReason,
)
from reclaim.contracts.events import EventType
from reclaim.contracts.money import Money
from reclaim.contracts.decline_taxonomy import DeclineClass
from reclaim.contracts.strata import StratumKey, legal_failure_classes_for

_T0 = datetime(2026, 3, 1, 4, 30, tzinfo=timezone.utc)


def _stratum() -> StratumKey:
    return StratumKey.build(
        amount=Money.from_rupees(1499),
        failure_class=RiskClass.FAILED_RECURRING_DEBIT,
        segment=Segment.B2C_STANDARD,
    )


def _evidence(**kw) -> EvidenceRef:
    kwargs = dict(
        evidence_id="ev_1",
        source_system="stripe_test",
        source_event_id="evt_3Nk9",
        event_type=EventType.PAYMENT_ATTEMPT,
        observed_at=_T0,
        summary="card_expired on attempt 1",
    )
    kwargs.update(kw)
    return EvidenceRef(**kwargs)


def _claim(**kw) -> Claim:
    kwargs = dict(
        statement="The stored card expired in Feb 2026.",
        evidence=[_evidence()],
    )
    kwargs.update(kw)
    return Claim(**kwargs)


def _diagnosis(**kw) -> Diagnosis:
    kwargs = dict(
        diagnosis_id="dx_1",
        case_id="case_1",
        root_cause=RootCauseClass.H2_CREDENTIAL_LIFECYCLE,
        confidence=Decimal("0.87"),
        source=DiagnosisSource.LLM,
        claims=[_claim()],
        reasoning_summary="Decline code and card expiry date agree.",
        created_at=_T0,
    )
    kwargs.update(kw)
    return Diagnosis(**kwargs)


def _case(**kw) -> RiskCase:
    kwargs = dict(
        case_id="case_1",
        obligation_id="obl_1",
        payer_id="payer_1",
        risk_class=RiskClass.FAILED_RECURRING_DEBIT,
        segment=Segment.B2C_STANDARD,
        amount_at_risk=Money.from_rupees(1499),
        detected_at=_T0,
        stratum=_stratum(),
        arm=Arm.A4,
        state=CaseState.DETECTED,
        recovery_window_ends_at=_T0 + timedelta(days=21),
    )
    kwargs.update(kw)
    return RiskCase(**kwargs)


# --------------------------------------------------------------- evidence


def test_a_claim_cannot_be_made_without_evidence():
    """§9.2 and the Recovery Receipt both require cited evidence. An
    uncited claim is the failure mode that loses a compliance review."""
    with pytest.raises(ValidationError):
        Claim(statement="I think they churned.", evidence=[])


def test_evidence_points_at_an_event_not_at_prose():
    evidence = _evidence()
    assert evidence.ingest_key == "stripe_test:evt_3Nk9"
    assert evidence.event_type is EventType.PAYMENT_ATTEMPT


# -------------------------------------------------------------- diagnosis


def test_diagnosis_confidence_is_a_quantised_decimal_not_a_float():
    """canonical_json rejects floats; the audit chain carries the confidence."""
    from reclaim.contracts.canonical import canonical_json

    assert canonical_json(_diagnosis(confidence=0.8712345678)).count('"0.871235"') == 1


def test_diagnosis_confidence_above_one_is_rejected():
    with pytest.raises(ValidationError):
        _diagnosis(confidence=Decimal("1.2"))


def test_an_llm_diagnosis_must_cite_at_least_one_claim():
    with pytest.raises(ValidationError):
        _diagnosis(claims=[])


def test_an_unknown_root_cause_forces_abstention():
    """§12.3 measures abstention rate. UNKNOWN is a legitimate answer, but it
    may not be dressed up with high confidence."""
    with pytest.raises(ValidationError):
        _diagnosis(root_cause=RootCauseClass.UNKNOWN, confidence=Decimal("0.95"))


def test_an_unknown_root_cause_with_low_confidence_is_accepted():
    diagnosis = _diagnosis(root_cause=RootCauseClass.UNKNOWN, confidence=Decimal("0.20"))
    assert diagnosis.is_abstention is True


def test_a_deterministic_fallback_diagnosis_needs_no_reasoning_prose():
    diagnosis = _diagnosis(source=DiagnosisSource.DETERMINISTIC_FALLBACK, reasoning_summary="")
    assert diagnosis.source is DiagnosisSource.DETERMINISTIC_FALLBACK


# ------------------------------------------------------------------- plan


def _step(**kw) -> PlanStep:
    kwargs = dict(
        step_index=0,
        trigger=StepTrigger.ALWAYS,
        action=ActionEnvelope(
            action_id="act_1",
            case_id="case_1",
            action=EscalateToHuman(queue=HumanQueue.APPROVALS, reason="needs a look"),
            proposed_by=PlanOrigin.LLM_PLANNER,
        ),
        earliest_at=_T0,
    )
    kwargs.update(kw)
    return PlanStep(**kwargs)


def _plan(**kw) -> Plan:
    kwargs = dict(
        plan_id="plan_1",
        case_id="case_1",
        diagnosis_id="dx_1",
        origin=PlanOrigin.LLM_PLANNER,
        steps=[_step()],
        created_at=_T0,
    )
    kwargs.update(kw)
    return Plan(**kwargs)


def test_a_plan_must_have_at_least_one_step():
    with pytest.raises(ValidationError):
        _plan(steps=[])


def test_plan_step_indices_must_be_contiguous_from_zero():
    """A gap means a step was dropped somewhere between the planner and here."""
    with pytest.raises(ValidationError):
        _plan(steps=[_step(step_index=0), _step(step_index=2)])


def test_the_first_step_cannot_depend_on_a_previous_step():
    with pytest.raises(ValidationError):
        _plan(steps=[_step(step_index=0, trigger=StepTrigger.PREV_FAILED)])


def test_plan_steps_must_belong_to_the_same_case_as_the_plan():
    """A cross-case action in a plan would execute against the wrong customer."""
    other = ActionEnvelope(
        action_id="act_9",
        case_id="case_999",
        action=EscalateToHuman(queue=HumanQueue.APPROVALS, reason="x"),
        proposed_by=PlanOrigin.LLM_PLANNER,
    )
    with pytest.raises(ValidationError):
        _plan(steps=[_step(action=other)])


def test_plan_is_bounded_in_length():
    with pytest.raises(ValidationError):
        _plan(steps=[_step(step_index=i) for i in range(40)])


def test_a_plan_exposes_the_strictest_tier_it_requires():
    debit = ActionEnvelope(
        action_id="act_2",
        case_id="case_1",
        action=ScheduleDebit(
            obligation_id="obl_1",
            mandate_id="mnd_1",
            rail=Rail.CARD_EMANDATE,
            amount=Money.from_rupees(1499),
            execute_at=_T0 + timedelta(days=2),
            pre_debit_notification_id="ntf_1",
            attempt_sequence=2,
        ),
        proposed_by=PlanOrigin.LLM_PLANNER,
    )
    plan = _plan(steps=[_step(step_index=0), _step(step_index=1, action=debit)])
    assert plan.catalog_tier_floor is AutonomyTier.T0
    assert plan.step_count == 2


# ------------------------------------------------------------------- case


def test_a_case_records_its_arm_and_stratum_at_creation():
    """§12.1: assignment is logged at creation and immutable."""
    case = _case()
    assert case.arm is Arm.A4
    assert case.stratum.amount_band.value == "le_2k"
    with pytest.raises(ValidationError):
        case.arm = Arm.A1


def test_a_case_recognises_amount_at_risk_once():
    """§13's anti-double-counting rule: at-risk is recognised once per
    obligation, at detection."""
    case = _case()
    assert case.amount_at_risk == Money.from_rupees(1499)
    assert case.at_risk_recognised_at == case.detected_at


def test_a_terminal_case_must_carry_a_stop_reason_when_it_stopped():
    with pytest.raises(ValidationError):
        _case(state=CaseState.STOPPED, stop_reason=None)


def test_a_non_terminal_case_may_not_carry_a_stop_reason():
    with pytest.raises(ValidationError):
        _case(state=CaseState.DIAGNOSING, stop_reason=StopReason.CONTACT_CAP)


def test_a_recovered_case_needs_no_stop_reason():
    case = _case(state=CaseState.RECOVERED)
    assert case.is_terminal is True


def test_case_state_transitions_are_validated_against_the_table():
    case = _case()
    assert case.can_transition_to(CaseState.DIAGNOSING) is True
    assert case.can_transition_to(CaseState.RECOVERED) is False


def test_recovery_window_must_end_after_detection():
    with pytest.raises(ValidationError):
        _case(recovery_window_ends_at=_T0 - timedelta(days=1))


def test_a_case_in_the_no_action_arm_may_not_hold_a_plan():
    """A0 measures natural recovery. A plan attached to an A0 case would
    silently contaminate the control floor."""
    with pytest.raises(ValidationError):
        _case(arm=Arm.A0, active_plan_id="plan_1")


# ------------------------------------------------------------------ audit


def _row(prev: AuditRow | None = None, **kw) -> AuditRow:
    kwargs = dict(
        sequence=0 if prev is None else prev.sequence + 1,
        ts=_T0,
        case_id="case_1",
        actor=ActorType.AGENT,
        event_type="policy_evaluated",
        inputs_digest="a" * 64,
        prev_hash=GENESIS_HASH if prev is None else prev.row_hash,
        model_id="claude-opus-5",
        prompt_version="1.0.0",
        policy_version="1.0.0",
        decision_rationale="Consent present, within quiet hours.",
    )
    kwargs.update(kw)
    return AuditRow(**kwargs)


def test_an_audit_row_hashes_itself():
    row = _row()
    assert len(row.row_hash) == 64
    assert row.prev_hash == GENESIS_HASH


def test_two_identical_rows_hash_identically():
    assert _row().row_hash == _row().row_hash


def test_changing_any_field_changes_the_row_hash():
    assert _row().row_hash != _row(decision_rationale="Something else").row_hash


def test_a_row_cannot_claim_a_hash_it_does_not_have():
    """A stored row is re-validated on read; a tampered hash must not parse."""
    row = _row()
    payload = row.model_dump(mode="json")
    payload["row_hash"] = "0" * 64
    with pytest.raises(ValidationError):
        AuditRow.model_validate(payload)


def test_a_row_round_trips_through_its_own_serialised_form():
    row = _row()
    assert AuditRow.model_validate(row.model_dump(mode="json")) == row


def test_verify_chain_accepts_a_well_formed_chain():
    first = _row()
    second = _row(prev=first, event_type="action_executed")
    third = _row(prev=second, event_type="payment_received")
    result = verify_chain([first, second, third])
    assert result.is_valid is True
    assert result.rows_checked == 3
    assert result.first_bad_sequence is None


def test_verify_chain_detects_a_broken_link():
    first = _row()
    second = _row(prev=first)
    forged = _row(prev=first, sequence=2, event_type="action_executed")
    result = verify_chain([first, second, forged])
    assert result.is_valid is False
    assert result.first_bad_sequence == 2
    assert "prev_hash" in result.reason


def test_verify_chain_detects_a_deleted_row():
    """Invariant: delete_audit_row does not exist. Deleting one anyway must be
    visible, which is the entire point of chaining."""
    first = _row()
    second = _row(prev=first)
    third = _row(prev=second)
    result = verify_chain([first, third])
    assert result.is_valid is False


def test_verify_chain_rejects_an_out_of_order_chain():
    first = _row()
    second = _row(prev=first)
    result = verify_chain([second, first])
    assert result.is_valid is False


def test_verify_chain_on_an_empty_log_is_valid_but_says_so():
    result = verify_chain([])
    assert result.is_valid is True
    assert result.rows_checked == 0


def test_the_first_row_may_not_point_anywhere_but_genesis():
    """Front-truncation defence. A row 0 whose prev_hash is some other row's hash
    means rows were lopped off the front, so it must not even construct -- which is
    stronger than verify_chain noticing, because every read path gets it."""
    with pytest.raises(ValidationError):
        _row(prev_hash="b" * 64)


def test_a_later_row_may_not_claim_to_be_genesis():
    with pytest.raises(ValidationError):
        _row(sequence=3, prev_hash=GENESIS_HASH)


def test_verify_chain_rejects_a_chain_that_does_not_start_at_zero():
    """The other half of front-truncation: rows 5..9 read from disk are each
    individually valid, and the chain still has to be refused."""
    first = _row()
    second = _row(prev=first)
    result = verify_chain([second])
    assert result.is_valid is False
    assert "sequence" in result.reason


def test_append_row_links_to_the_tail_automatically():
    first = _row()
    second = append_row(
        [first],
        ts=_T0 + timedelta(minutes=1),
        case_id="case_1",
        actor=ActorType.SYSTEM,
        event_type="action_executed",
        inputs_digest="c" * 64,
        model_id="claude-opus-5",
        prompt_version="1.0.0",
        policy_version="1.0.0",
        decision_rationale="Executed.",
    )
    assert second.prev_hash == first.row_hash
    assert second.sequence == 1
    assert verify_chain([first, second]).is_valid


def test_an_executed_action_row_carries_its_idempotency_key():
    """Invariant #8: every external action has exactly one audit row and one
    idempotency key."""
    row = _row(event_type="action_executed", idempotency_key="schedule_debit:" + "f" * 64)
    assert row.idempotency_key is not None


def test_a_policy_row_records_every_verdict_including_allows():
    row = _row(policy_verdict_rule_ids=("POL-A-001", "POL-D-002"))
    assert row.policy_verdict_rule_ids == ("POL-A-001", "POL-D-002")


def test_a_fully_populated_row_hashes_without_a_float_reaching_the_chain():
    """The real risk is not a float typed into AuditRow -- every numeric field here
    is an int -- but a float smuggled in via a nested model. This row carries the
    two nestings that exist: an action envelope and a policy decision."""
    from reclaim.contracts.canonical import canonical_json
    from reclaim.contracts.enums import PolicyCategory, PolicyEffect
    from reclaim.contracts.policy_format import PolicyVerdict, combine_verdicts

    envelope = ActionEnvelope(
        action_id="act_7",
        case_id="case_1",
        action=ScheduleDebit(
            obligation_id="obl_1",
            mandate_id="mnd_1",
            rail=Rail.CARD_EMANDATE,
            amount=Money.from_rupees(1499),
            execute_at=_T0 + timedelta(days=2),
            pre_debit_notification_id="ntf_1",
            attempt_sequence=2,
        ),
        proposed_by=PlanOrigin.LLM_PLANNER,
    )
    decision = combine_verdicts(
        [
            PolicyVerdict(
                rule_id="POL-TIMING-001",
                category=PolicyCategory.TIMING,
                effect=PolicyEffect.ALLOW,
                human_reason="within the send window",
            )
        ]
    )
    row = _row(
        event_type="action_executed",
        tool_call=envelope,
        idempotency_key=envelope.idempotency_key,
        tool_result_digest="d" * 64,
        policy_verdict_rule_ids=("POL-TIMING-001",),
        policy_decision=decision,
    )
    canonical_json(row.model_dump(mode="json"))
    assert verify_chain([row]).is_valid is True


def test_a_row_carrying_an_action_must_carry_that_actions_key():
    """Invariant #8 read strictly: the log and the executor's dedupe table must not
    be able to disagree about which act a row records."""
    envelope = ActionEnvelope(
        action_id="act_8",
        case_id="case_1",
        action=EscalateToHuman(queue=HumanQueue.APPROVALS, reason="x"),
        proposed_by=PlanOrigin.LLM_PLANNER,
    )
    with pytest.raises(ValidationError):
        _row(event_type="action_executed", tool_call=envelope, idempotency_key=None)
    with pytest.raises(ValidationError):
        _row(
            event_type="action_executed",
            tool_call=envelope,
            idempotency_key="escalate_to_human:" + "0" * 64,
        )


def test_a_tampered_middle_row_breaks_the_chain_on_read():
    """The end-to-end property: edit a written row's rationale, and the log stops
    verifying. This is what `verify_chain` demonstrates to a reviewer."""
    first = _row()
    second = _row(prev=first)
    third = _row(prev=second)
    stored = [r.model_dump(mode="json") for r in (first, second, third)]
    stored[1]["decision_rationale"] = "Edited after the fact."
    with pytest.raises(ValidationError):
        AuditRow.model_validate(stored[1])


def test_a_stratum_whose_amount_band_disagrees_with_the_case_is_rejected():
    """JC-23 stores the stratum rather than deriving it, on the argument that it
    must have been derived from *this* case when it was stored. Checking only the
    segment leaves the band unchecked: a case can carry a stratum from a different
    amount band, construct cleanly, and then be counted in the wrong bucket by
    stratum_weighted_incremental_recovery -- which is the headline number."""
    from_a_different_band = StratumKey.build(
        amount=Money.from_rupees(500000),
        failure_class=RiskClass.FAILED_RECURRING_DEBIT,
        segment=Segment.B2C_STANDARD,
    )
    with pytest.raises(ValidationError):
        _case(stratum=from_a_different_band)  # the case itself is Rs 1,499


def test_a_stratum_whose_failure_class_disagrees_with_the_case_is_rejected():
    """Same hole, other axis. A stratum's failure class is what makes an arm
    comparison like-for-like."""
    from_a_different_class = StratumKey.build(
        amount=Money.from_rupees(1499),
        failure_class=RiskClass.OVERDUE_RECEIVABLE,
        segment=Segment.B2C_STANDARD,
    )
    with pytest.raises(ValidationError):
        _case(stratum=from_a_different_class)


# ------------------------------------------------- decline class (JC-42/Q10)


def _d1_case(decline_class, **kw) -> RiskCase:
    """A D1 case stratified on ``decline_class``, the way a detector builds one.

    ``canonical_decline_class`` defaults to the same class and is overridable, so a
    test can construct the disagreeing pair the validator must reject."""
    kwargs = dict(
        canonical_decline_class=decline_class,
        stratum=StratumKey.build(
            amount=Money.from_rupees(1499),
            failure_class=decline_class,
            segment=Segment.B2C_STANDARD,
        ),
    )
    kwargs.update(kw)
    return _case(**kwargs)


def test_a_failed_debit_case_may_stratify_on_its_decline_class():
    """The defect JC-42 fixed. ``StratumKey.failure_class`` documents two legal
    vocabularies, but the case validator required the RiskClass one on every case,
    so the DeclineClass half was unreachable and no test noticed -- every fixture
    in the suite passed a RiskClass on every axis."""
    case = _d1_case(DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS)
    assert case.stratum.failure_class == "payer_authorization_missing_ambiguous"
    assert (
        case.canonical_decline_class
        is DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS
    )


def test_every_decline_class_is_legal_on_a_failed_debit_case():
    """The table is walked, not spot-checked. A per-class exclusion added later
    (an 'our-side classes never stratify' rule, say) must fail here rather than
    silently rejecting one detector's output at 3am."""
    for decline_class in DeclineClass:
        case = _d1_case(decline_class)
        assert case.canonical_decline_class is decline_class


def test_a_failed_debit_case_may_still_stratify_on_its_risk_class():
    """The pre-normalisation D1 case. The stratum freezes at detection (JC-23), so
    a case whose decline code has not been mapped yet must still be constructible."""
    case = _case(canonical_decline_class=None)
    assert case.stratum.failure_class == RiskClass.FAILED_RECURRING_DEBIT.value
    assert case.canonical_decline_class is None


def test_a_case_may_name_a_decline_class_its_frozen_stratum_predates():
    """The converse is deliberately allowed: normalisation can land after the
    stratum is frozen, and forbidding this would force a choice between a stale
    stratum and an unrecorded class."""
    case = _case(canonical_decline_class=DeclineClass.CARD_EXPIRED)
    assert case.stratum.failure_class == RiskClass.FAILED_RECURRING_DEBIT.value
    assert case.canonical_decline_class is DeclineClass.CARD_EXPIRED


def test_a_stratum_decline_class_the_case_does_not_name_is_rejected():
    """A bucket that cannot be traced back to an observation. The strata are what
    §12.1's headline is summed over, so an untraceable one is a weighting error."""
    with pytest.raises(ValidationError):
        _d1_case(DeclineClass.INSUFFICIENT_FUNDS, canonical_decline_class=None)


def test_a_decline_class_disagreeing_with_the_stratum_is_rejected():
    with pytest.raises(ValidationError):
        _d1_case(
            DeclineClass.INSUFFICIENT_FUNDS,
            canonical_decline_class=DeclineClass.CARD_EXPIRED,
        )


def test_only_a_failed_debit_case_carries_a_decline_class():
    """D2-D6 do not observe a PSP decline code. D2 in particular is *not* covered
    by JC-42: a predicted failure has no decline code yet, and whether a predicted
    class belongs in a stratum is the D2 detector's question (CONTRACTS.md Q10)."""
    for risk_class in RiskClass:
        if risk_class is RiskClass.FAILED_RECURRING_DEBIT:
            continue
        stratum = StratumKey.build(
            amount=Money.from_rupees(1499),
            failure_class=risk_class,
            segment=Segment.B2C_STANDARD,
        )
        # The risk-class stratification is still legal ...
        assert _case(
            risk_class=risk_class, stratum=stratum, canonical_decline_class=None
        )
        # ... but naming a decline class is not.
        with pytest.raises(ValidationError):
            _case(
                risk_class=risk_class,
                stratum=stratum,
                canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
            )


def test_a_non_d1_case_may_not_stratify_on_a_decline_class():
    """Including D2. The flat ``legal_failure_classes()`` union accepts the value,
    so StratumKey itself cannot catch this -- the case is the only place that knows
    which risk class produced the stratum."""
    stratum = StratumKey.build(
        amount=Money.from_rupees(1499),
        failure_class=DeclineClass.INSUFFICIENT_FUNDS,
        segment=Segment.B2C_STANDARD,
    )
    for risk_class in RiskClass:
        if risk_class is RiskClass.FAILED_RECURRING_DEBIT:
            continue
        with pytest.raises(ValidationError):
            _case(risk_class=risk_class, stratum=stratum)


def test_legal_failure_classes_is_scoped_per_risk_class():
    """``legal_failure_classes()`` is a flat union by design (a StratumKey does not
    know its risk class); ``legal_failure_classes_for`` is the scoped one."""
    d1 = legal_failure_classes_for(RiskClass.FAILED_RECURRING_DEBIT)
    assert {c.value for c in DeclineClass} <= d1
    assert RiskClass.FAILED_RECURRING_DEBIT.value in d1
    assert RiskClass.OVERDUE_RECEIVABLE.value not in d1
    for risk_class in RiskClass:
        if risk_class is RiskClass.FAILED_RECURRING_DEBIT:
            continue
        assert legal_failure_classes_for(risk_class) == frozenset({risk_class.value})


def test_the_decline_class_survives_a_serialisation_round_trip():
    """The field reaches ``risk_cases.data`` and the receipt through model_dump.

    Computed fields are stripped before re-validation exactly as
    ``spine.case_machine._rebuild`` does -- they are outputs, not settable inputs."""
    case = _d1_case(DeclineClass.MANDATE_CANCELLED)
    payload = case.model_dump(
        mode="json", exclude=set(RiskCase.model_computed_fields)
    )
    assert payload["canonical_decline_class"] == "mandate_cancelled"
    assert (
        RiskCase.model_validate(payload).canonical_decline_class
        is DeclineClass.MANDATE_CANCELLED
    )


def test_every_live_state_can_stop_immediately():
    """§14.3 requires an opt-out or dispute to stop a case "immediately and
    permanently". A live state with no edge to STOPPED cannot honour that: the
    case must first transition somewhere else, which means one more action after
    the customer said stop. That is the violation, not a delay."""
    for state, allowed in ALLOWED_CASE_TRANSITIONS.items():
        if not allowed:
            continue  # terminal by construction
        assert CaseState.STOPPED in allowed, (
            f"{state.value} has no edge to stopped, so an opt-out arriving in "
            f"that state cannot be honoured immediately"
        )
