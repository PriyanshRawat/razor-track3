"""Contract tests for the typed action catalog (deliverable #3).

The catalog is the narrowest point in the system: the LLM can only *propose*
members of this union, the policy engine vetoes members of this union, and the
executor executes members of this union. If a verb is not here, it does not
exist. These tests pin that property rather than merely exercising the models.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from reclaim.contracts.actions import (
    ACTION_SPECS,
    ACTION_MODELS,
    FORBIDDEN_VERBS,
    Action,
    ActionEnvelope,
    ActionType,
    ApplyGracePeriod,
    CreateCredentialUpdateLink,
    CreateMandateReauthLink,
    EscalateToHuman,
    InitiateVoiceCall,
    OfferPaymentPlan,
    OpenSystemicIncident,
    PaymentPlanInstalment,
    ProposeRouteChange,
    RecommendWriteOff,
    ScheduleDebit,
    SendMessage,
    SendPreDebitNotification,
    SuppressContact,
    TemplateSlotValue,
    action_spec,
    idempotency_key,
    tool_schemas_for_llm,
)
from reclaim.contracts.canonical import canonical_json, digest
from reclaim.contracts.enums import (
    AutonomyTier,
    Channel,
    HumanQueue,
    Language,
    MessageIntent,
    PolicyCategory,
    Rail,
    Reversibility,
    SuppressionScope,
)
from reclaim.contracts.money import Money

_T0 = datetime(2026, 3, 1, 4, 30, tzinfo=timezone.utc)


def _schedule_debit(**overrides) -> ScheduleDebit:
    kwargs = dict(
        obligation_id="obl_1001",
        mandate_id="mnd_77",
        rail=Rail.CARD_EMANDATE,
        execute_at=_T0 + timedelta(days=2),
        amount=Money.from_rupees(1499),
        pre_debit_notification_id="ntf_5",
        attempt_sequence=2,
    )
    kwargs.update(overrides)
    return ScheduleDebit(**kwargs)


# ------------------------------------------------- the catalog is the boundary


def test_every_action_type_has_a_model_and_a_spec():
    assert set(ACTION_MODELS) == set(ActionType)
    assert set(ACTION_SPECS) == set(ActionType)


def test_the_catalog_has_exactly_the_thirteen_verbs_in_plan_section_10_2():
    assert len(ActionType) == 13


def test_forbidden_verbs_are_absent_from_the_catalog():
    """§14.4: 'the dangerous verbs do not exist'. This is a security property,
    so it is asserted, not assumed."""
    catalog = {t.value for t in ActionType}
    for verb in FORBIDDEN_VERBS:
        assert verb not in catalog, f"{verb} must never be an executable verb"
    assert "mark_invoice_paid" in FORBIDDEN_VERBS
    assert "apply_discount" in FORBIDDEN_VERBS


def test_an_unknown_verb_cannot_be_parsed_into_the_action_union():
    """The LLM emitting a verb we never defined must fail validation, not
    degrade into a generic action."""

    class _Holder(BaseModel):
        action: Action

    with pytest.raises(ValidationError):
        _Holder.model_validate({"action": {"action_type": "mark_invoice_paid"}})


def test_action_model_rejects_unknown_fields():
    """An LLM hallucinating an extra parameter (e.g. discount_pct) must fail
    rather than have it silently dropped."""
    with pytest.raises(ValidationError):
        _schedule_debit(discount_pct=10)


# ------------------------------------------------------------------ specs


def test_schedule_debit_is_irreversible_and_moves_money():
    spec = action_spec(ActionType.SCHEDULE_DEBIT)
    assert spec.moves_money is True
    assert spec.reversibility is Reversibility.IRREVERSIBLE
    assert spec.customer_visible is True


def test_recommend_write_off_is_t3_advice_only():
    spec = action_spec(ActionType.RECOMMEND_WRITE_OFF)
    assert spec.base_tier is AutonomyTier.T3
    assert spec.is_recommendation_only is True
    assert spec.moves_money is False


def test_escalate_to_human_is_always_allowed_and_is_the_safe_default():
    spec = action_spec(ActionType.ESCALATE_TO_HUMAN)
    assert spec.base_tier is AutonomyTier.T0
    assert spec.always_permitted is True
    assert spec.customer_visible is False


def test_only_escalation_is_always_permitted():
    always = {t for t, s in ACTION_SPECS.items() if s.always_permitted}
    assert always == {ActionType.ESCALATE_TO_HUMAN}


def test_offer_payment_plan_and_voice_call_are_t2_always():
    assert action_spec(ActionType.OFFER_PAYMENT_PLAN).base_tier is AutonomyTier.T2
    assert action_spec(ActionType.OFFER_PAYMENT_PLAN).never_below_base_tier is True
    assert action_spec(ActionType.INITIATE_VOICE_CALL).base_tier is AutonomyTier.T2
    assert action_spec(ActionType.INITIATE_VOICE_CALL).never_below_base_tier is True


def test_propose_route_change_is_t2_and_never_auto():
    spec = action_spec(ActionType.PROPOSE_ROUTE_CHANGE)
    assert spec.base_tier is AutonomyTier.T2
    assert spec.never_below_base_tier is True
    assert spec.is_config_change is True


def test_every_outbound_contact_is_governed_by_consent_timing_and_frequency():
    """§14.1 applies as a set: an action that puts a message in front of a
    customer needs all three gates, or one of them will be forgotten."""
    for action_type, spec in ACTION_SPECS.items():
        if spec.is_outbound_contact:
            assert spec.requires_consent, f"{action_type} contacts without consent"
            assert spec.requires_quiet_hours_check, f"{action_type} contacts outside quiet-hours control"
            assert spec.counts_toward_frequency_cap, f"{action_type} evades the frequency cap"
            assert spec.channel_field is not None, f"{action_type} contacts on no declared channel"


def test_a_debit_is_customer_visible_but_is_not_an_outbound_contact():
    """The distinction matters: a debit is authorised by a mandate, not by
    contact consent, and it must not consume the contact frequency cap."""
    spec = action_spec(ActionType.SCHEDULE_DEBIT)
    assert spec.customer_visible is True
    assert spec.is_outbound_contact is False
    assert spec.counts_toward_frequency_cap is False
    assert spec.requires_valid_mandate is True


def test_reconciliation_is_required_before_money_movement_and_before_contact():
    """§14.3: 'reconciliation check runs before every contact'. A customer who
    has already paid must never be debited or chased."""
    for action_type, spec in ACTION_SPECS.items():
        if spec.is_outbound_contact or spec.moves_money:
            assert spec.requires_reconciliation_check, f"{action_type} skips the already-paid check"


def test_internal_actions_need_no_consent():
    for action_type in (
        ActionType.OPEN_SYSTEMIC_INCIDENT,
        ActionType.SUPPRESS_CONTACT,
        ActionType.ESCALATE_TO_HUMAN,
        ActionType.RECOMMEND_WRITE_OFF,
        ActionType.PROPOSE_ROUTE_CHANGE,
    ):
        spec = action_spec(action_type)
        assert spec.is_outbound_contact is False
        assert spec.requires_consent is False


# ------------------------------------------------- financial authority = zero


def test_no_action_can_carry_a_concession_value():
    """Invariant #7: agent-granted concession value = Rs 0. No action in the
    catalog exposes a discount, waiver or write-down parameter."""
    banned_params = {"discount", "discount_pct", "waiver", "waive", "write_down", "concession", "credit_amount"}
    for action_type, model in ACTION_MODELS.items():
        fields = set(model.model_fields)
        assert not (fields & banned_params), f"{action_type} exposes a concession parameter"


def test_payment_plan_instalments_must_sum_to_the_full_amount():
    """A plan that reschedules Rs 10,000 as Rs 9,000 is a disguised discount."""
    with pytest.raises(ValidationError):
        OfferPaymentPlan(
            obligation_id="obl_1",
            total_amount=Money.from_rupees(10000),
            instalments=[
                PaymentPlanInstalment(due_at=_T0, amount=Money.from_rupees(5000)),
                PaymentPlanInstalment(due_at=_T0 + timedelta(days=30), amount=Money.from_rupees(4000)),
            ],
        )


def test_a_payment_plan_that_sums_exactly_is_accepted():
    plan = OfferPaymentPlan(
        obligation_id="obl_1",
        total_amount=Money.from_rupees(10000),
        instalments=[
            PaymentPlanInstalment(due_at=_T0, amount=Money.from_rupees(5000)),
            PaymentPlanInstalment(due_at=_T0 + timedelta(days=30), amount=Money.from_rupees(5000)),
        ],
    )
    assert plan.instalment_count == 2


# ------------------------------------------------------ rail-shape validation


def test_schedule_debit_is_structurally_impossible_on_a_push_only_rail():
    """Bank transfer money can only be pushed to us. The type system, not a
    policy rule, refuses the debit."""
    with pytest.raises(ValidationError):
        _schedule_debit(rail=Rail.BANK_TRANSFER)


def test_schedule_debit_on_a_notifying_rail_requires_a_notification_reference():
    """Invariant #4 has a structural half: you cannot even *express* an India
    e-mandate debit without pointing at the notification that preceded it."""
    with pytest.raises(ValidationError):
        _schedule_debit(pre_debit_notification_id=None)


def test_one_time_card_needs_no_notification_reference():
    action = _schedule_debit(
        rail=Rail.CARD_ONE_TIME, pre_debit_notification_id=None, mandate_id=None
    )
    assert action.pre_debit_notification_id is None


def test_schedule_debit_rejects_a_zero_or_negative_amount():
    with pytest.raises(ValidationError):
        _schedule_debit(amount=Money.from_rupees(0))


def test_pre_debit_notification_must_carry_an_exact_amount_and_a_debit_time():
    """§11.2: the notification must state the exact amount. It is a required
    field, so an amount-less notification cannot be constructed."""
    assert "amount" in SendPreDebitNotification.model_fields
    assert "debit_scheduled_at" in SendPreDebitNotification.model_fields
    with pytest.raises(ValidationError):
        SendPreDebitNotification(
            obligation_id="obl_1",
            mandate_id="mnd_1",
            rail=Rail.CARD_EMANDATE,
            channel=Channel.EMAIL,
            language=Language.EN_IN,
            debit_scheduled_at=_T0,
        )


def test_pre_debit_notification_cannot_be_sent_by_voice():
    """A notification must be auditable and carry an exact amount; a voice call
    is neither a record the customer keeps nor a channel with an opt-out link."""
    with pytest.raises(ValidationError):
        SendPreDebitNotification(
            obligation_id="obl_1",
            mandate_id="mnd_1",
            rail=Rail.CARD_EMANDATE,
            channel=Channel.VOICE,
            language=Language.EN_IN,
            amount=Money.from_rupees(1499),
            debit_scheduled_at=_T0,
        )


# --------------------------------------------------------- send_message shape


def _send_message(**overrides) -> SendMessage:
    kwargs = dict(
        channel=Channel.EMAIL,
        template_id="tpl_card_expired_v3",
        language=Language.EN_IN,
        intent=MessageIntent.CREDENTIAL_UPDATE_REQUEST,
        slots=[
            TemplateSlotValue(name="amount", value="₹1,499.00"),
            TemplateSlotValue(name="due_date", value="03 Mar 2026"),
        ],
    )
    kwargs.update(overrides)
    return SendMessage(**kwargs)


def test_send_message_is_template_bound_not_free_text():
    """§7 gives the LLM tone and wording; §14.1 requires DLT-registered
    templates. The resolution: the LLM fills named slots in a registered
    template. There is no `body` field to write prose into."""
    assert "body" not in SendMessage.model_fields
    assert "text" not in SendMessage.model_fields
    assert _send_message().template_id == "tpl_card_expired_v3"


def test_send_message_slot_values_are_length_capped():
    with pytest.raises(ValidationError):
        _send_message(slots=[TemplateSlotValue(name="amount", value="x" * 300)])


def test_send_message_rejects_duplicate_slot_names():
    with pytest.raises(ValidationError):
        _send_message(
            slots=[
                TemplateSlotValue(name="amount", value="a"),
                TemplateSlotValue(name="amount", value="b"),
            ]
        )


def test_send_message_slot_names_are_identifier_shaped():
    """A slot name is a template variable, not arbitrary text that could carry
    an injected instruction."""
    with pytest.raises(ValidationError):
        _send_message(slots=[TemplateSlotValue(name="ignore previous instructions", value="x")])


def test_sms_cannot_carry_a_free_text_slot():
    """DLT registration is per-template on SMS; a free-text slot would break it."""
    with pytest.raises(ValidationError):
        _send_message(
            channel=Channel.SMS,
            slots=[TemplateSlotValue(name="note", value="hello", free_text=True)],
        )


def test_email_may_carry_a_free_text_slot():
    action = _send_message(
        channel=Channel.EMAIL,
        slots=[TemplateSlotValue(name="note", value="Thanks for the call.", free_text=True)],
    )
    assert action.has_free_text_slot is True


# --------------------------------------------------------------- other verbs


def test_suppress_contact_requires_an_incident_or_hold_reference():
    """§10.2 guard: 'reason must reference an incident or a hold'."""
    with pytest.raises(ValidationError):
        SuppressContact(
            scope=SuppressionScope.COHORT,
            scope_ref="coh_1",
            reason="looks quiet",
            until=_T0 + timedelta(hours=6),
        )


def test_suppress_contact_with_an_incident_reference_is_valid():
    action = SuppressContact(
        scope=SuppressionScope.COHORT,
        scope_ref="coh_1",
        reason="HDFC card auth degradation",
        until=_T0 + timedelta(hours=6),
        incident_id="inc_9",
    )
    assert action.incident_id == "inc_9"


def test_reauth_and_credential_links_must_expire():
    link = CreateMandateReauthLink(payer_id="payer_1", rail=Rail.UPI_AUTOPAY, expires_at=_T0 + timedelta(days=3))
    assert link.expires_at > _T0
    with pytest.raises(ValidationError):
        CreateCredentialUpdateLink(payer_id="payer_1")


def test_grace_period_days_are_bounded():
    with pytest.raises(ValidationError):
        ApplyGracePeriod(obligation_id="obl_1", days=400, rationale="long")
    assert ApplyGracePeriod(obligation_id="obl_1", days=14, rationale="ok").days == 14


def test_voice_call_requires_disclosure_and_recording_notice():
    with pytest.raises(ValidationError):
        InitiateVoiceCall(
            payer_id="payer_1",
            script_id="scr_1",
            language=Language.HI_IN,
            discloses_automated_call=False,
            gives_recording_notice=True,
        )


def test_open_systemic_incident_carries_a_cohort_and_hypothesis():
    action = OpenSystemicIncident(cohort_id="coh_1", hypothesis="HDFC auth rate halved")
    assert action.cohort_id == "coh_1"


def test_escalate_to_human_requires_a_queue_and_a_reason():
    action = EscalateToHuman(queue=HumanQueue.APPROVALS, reason="confidence below floor twice")
    assert action.queue is HumanQueue.APPROVALS


def test_recommend_write_off_carries_no_execution_parameters():
    action = RecommendWriteOff(obligation_id="obl_1", rationale="uncollectable, 180d aged")
    assert "amount" not in RecommendWriteOff.model_fields
    assert action.action_type is ActionType.RECOMMEND_WRITE_OFF


def test_propose_route_change_requires_a_rollback_plan():
    with pytest.raises(ValidationError):
        ProposeRouteChange(
            cohort_id="coh_1",
            change_description="route HDFC via PSP2",
            rollback_plan="",
        )


# ---------------------------------------------------------- idempotency keys


def test_logically_identical_actions_produce_the_same_idempotency_key():
    """Invariant #1. Two proposals with the same parameters are the same act."""
    assert idempotency_key(_schedule_debit(), "case_1") == idempotency_key(_schedule_debit(), "case_1")


def test_a_different_amount_produces_a_different_idempotency_key():
    a = idempotency_key(_schedule_debit(), "case_1")
    b = idempotency_key(_schedule_debit(amount=Money.from_rupees(1500)), "case_1")
    assert a != b


def test_the_same_action_in_two_cases_produces_different_idempotency_keys():
    a = idempotency_key(_schedule_debit(), "case_1")
    b = idempotency_key(_schedule_debit(), "case_2")
    assert a != b


def test_slot_ordering_does_not_change_the_idempotency_key():
    """Canonical JSON sorts keys; slots are sorted by name so that an LLM
    emitting them in a different order does not double-send a message."""
    forward = _send_message(
        slots=[TemplateSlotValue(name="amount", value="1"), TemplateSlotValue(name="due_date", value="2")]
    )
    reverse = _send_message(
        slots=[TemplateSlotValue(name="due_date", value="2"), TemplateSlotValue(name="amount", value="1")]
    )
    assert idempotency_key(forward, "case_1") == idempotency_key(reverse, "case_1")


def test_idempotency_key_is_prefixed_by_the_action_type_for_readability():
    key = idempotency_key(_schedule_debit(), "case_1")
    assert key.startswith("schedule_debit:")


# ------------------------------------------------------------------ envelope


def test_action_envelope_is_canonically_serialisable():
    envelope = ActionEnvelope(
        action_id="act_1",
        case_id="case_1",
        action=_schedule_debit(),
        proposed_by="llm_planner",
    )
    assert digest(envelope) == digest(envelope.model_copy(deep=True))
    assert '"action_type":"schedule_debit"' in canonical_json(envelope)


def test_action_envelope_derives_its_own_idempotency_key():
    envelope = ActionEnvelope(
        action_id="act_1", case_id="case_1", action=_schedule_debit(), proposed_by="llm_planner"
    )
    assert envelope.idempotency_key == idempotency_key(_schedule_debit(), "case_1")


def test_action_envelope_rejects_a_supplied_idempotency_key():
    """Invariant #8: exactly one idempotency key per action, derived not
    asserted. A caller (or an LLM) must not be able to set it."""
    with pytest.raises(ValidationError):
        ActionEnvelope(
            action_id="act_1",
            case_id="case_1",
            action=_schedule_debit(),
            proposed_by="llm_planner",
            idempotency_key="whatever-i-like",
        )


def test_action_envelope_round_trips_through_its_own_serialised_form():
    """§15 decision replay reads rows back. An envelope that cannot be parsed
    from its own JSON makes the audit log unreplayable."""
    envelope = ActionEnvelope(
        action_id="act_1", case_id="case_1", action=_schedule_debit(), proposed_by="llm_planner"
    )
    revived = ActionEnvelope.model_validate(envelope.model_dump(mode="json"))
    assert revived == envelope
    assert revived.idempotency_key == envelope.idempotency_key


def test_a_tampered_idempotency_key_is_rejected_on_replay():
    """The derived key is the tamper check: a row whose stored key disagrees
    with its own parameters must not silently re-derive a clean one."""
    envelope = ActionEnvelope(
        action_id="act_1", case_id="case_1", action=_schedule_debit(), proposed_by="llm_planner"
    )
    row = envelope.model_dump(mode="json")
    row["idempotency_key"] = "schedule_debit:" + "0" * 64
    with pytest.raises(ValidationError):
        ActionEnvelope.model_validate(row)


# ------------------------------------------------------------- LLM tool defs


def test_llm_tool_schemas_cover_the_catalog_and_nothing_else():
    schemas = tool_schemas_for_llm()
    assert {s["name"] for s in schemas} == {t.value for t in ActionType}


def test_llm_tool_schemas_are_json_serialisable_without_floats():
    """These go into a tool definition and into the prompt hash."""
    canonical_json(tool_schemas_for_llm())


def test_every_declared_channel_field_exists_on_its_action_model():
    """§14.1's consent, quiet-hours and frequency gates all read the channel off
    the action via ``spec.channel_field``. A spec naming a field its model does not
    have turns every one of those lookups into an AttributeError -- on an action
    whose whole point is that it contacts a human being. Asserting the field is
    merely ``not None`` does not catch it."""
    for action_type, spec in ACTION_SPECS.items():
        if spec.channel_field is None:
            continue
        model = ACTION_MODELS[action_type]
        assert spec.channel_field in model.model_fields, (
            f"{action_type.value} declares channel_field="
            f"{spec.channel_field!r} but {model.__name__} has no such field"
        )


# ---------------------------------------- N6: the money-moving verb's categories


def test_schedule_debit_is_gated_on_financial_authority():
    """CONTRACTS.md §7 N6. ``policy_categories`` is what a generic engine
    iterates to decide which rules to evaluate, and the only money-moving verb in
    the catalog named RAIL, HOLDS and INTEGRITY while its own ``guard_summary``
    promised "T2 above the AFA threshold". The financial-authority rules were
    therefore never asked for on the one action that moves money."""
    spec = action_spec(ActionType.SCHEDULE_DEBIT)
    assert PolicyCategory.FINANCIAL_AUTHORITY in spec.policy_categories
    assert "AFA" in spec.guard_summary


def test_every_money_moving_action_is_gated_on_financial_authority():
    """Stated over the catalog rather than about ``schedule_debit``, so the next
    money-moving verb inherits the gate instead of repeating the defect."""
    movers = [t for t, s in ACTION_SPECS.items() if s.moves_money]
    assert movers, "no money-moving action; this test would assert nothing"
    for action_type in movers:
        assert (
            PolicyCategory.FINANCIAL_AUTHORITY
            in ACTION_SPECS[action_type].policy_categories
        ), f"{action_type} moves money with no financial-authority gate"


def test_the_pre_debit_notification_window_is_covered_by_the_rail_category():
    """The other half of N6, and the half that needed no change. §14.1 files
    "pre-debit notification >=24h before any India debit" under **Rail /
    network**, not under Timing -- the lead time is a rail mechanic, cited in
    ``rails.py``, not a number we choose."""
    spec = action_spec(ActionType.SCHEDULE_DEBIT)
    assert PolicyCategory.RAIL_AND_NETWORK in spec.policy_categories
    assert "pre-debit notification >=24h" in spec.guard_summary


def test_a_debit_is_not_gated_on_timing_because_timing_is_contact_scoped():
    """Pinned because the obvious reading of N6 -- "add both missing categories"
    -- would make a mandate-authorised debit consult quiet hours. Every rule in
    §14.1's Timing row (quiet hours, declared holidays, contact windows) is scoped
    to *contact*, and a debit is not contact."""
    spec = action_spec(ActionType.SCHEDULE_DEBIT)
    assert spec.is_outbound_contact is False
    assert spec.requires_quiet_hours_check is False
    assert PolicyCategory.TIMING not in spec.policy_categories


def test_timing_is_evaluated_for_exactly_the_quiet_hours_checked_verbs():
    """Walks the catalog to say what the assertion above relies on: TIMING and
    ``requires_quiet_hours_check`` are two spellings of one decision, and an
    action carrying one without the other is a gate that half exists."""
    for action_type, spec in ACTION_SPECS.items():
        assert (PolicyCategory.TIMING in spec.policy_categories) == (
            spec.requires_quiet_hours_check
        ), f"{action_type} disagrees with itself about timing"


def test_every_policy_category_named_by_a_spec_is_a_real_category():
    """The registry walked as a whole. ``policy_categories`` is the only place an
    action names policy rules, so a category that is not in §14.1's eight is a
    set of rules nothing will ever supply."""
    for action_type, spec in ACTION_SPECS.items():
        assert spec.policy_categories, f"{action_type} asks for no policy at all"
        for category in spec.policy_categories:
            assert isinstance(category, PolicyCategory)
