"""Phase 1: assembling the closed fact vocabulary from real state.

A rule can only be as honest as the fact it reads. Three failure modes are what
this file exists to catch, and all three are silent:

* **A fact the builder cannot supply.** The rule then never fires, and the engine
  reports the category as silent rather than the rule as broken. The coverage
  test walks every governed action type and asserts the bundle carries every fact
  the applicable rules reference -- so a rule added against an unsourced fact
  fails here, not in production.
* **A fact of the wrong Python type.** ``rail eq "card_one_time"`` compares a
  string against a ``Rail`` member; ``debit_amount gt 15000`` compares a ``Money``
  against an int. Both read correctly. ``validate_facts`` walks ``FACT_TYPES`` and
  refuses.
* **Quiet hours evaluated in the wrong zone.** JC-43 put the *precedence* in one
  place and said plainly that the arithmetic is still the engine's to get wrong.
  This is where the arithmetic is pinned, including in a zone that is not the
  server's and not the fallback.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reclaim.contracts.actions import (
    ActionEnvelope,
    ActionType,
    ScheduleDebit,
    SendMessage,
    TemplateSlotValue,
)
from reclaim.contracts.decline_taxonomy import DeclineClass
from reclaim.contracts.enums import (
    Arm,
    CaseState,
    Channel,
    Language,
    MandateStatus,
    MessageIntent,
    ObligationKind,
    ObligationStatus,
    PlanOrigin,
    PspId,
    Rail,
    RiskClass,
    Segment,
    StopReason,
)
from reclaim.contracts.case import RiskCase
from reclaim.contracts.money import Money
from reclaim.contracts.obligations import (
    ConsentProfile,
    ConsentRecord,
    Hold,
    Mandate,
    Obligation,
    QuietHours,
)
from reclaim.contracts.policy_format import (
    FALLBACK_QUIET_HOURS_TIMEZONE,
    PolicyFactKey as F,
    PolicyThresholds,
)
from reclaim.contracts.strata import StratumKey
from reclaim.policy.engine import applicable_rules
from reclaim.policy.facts import (
    FactContext,
    build_facts,
    contact_window_end,
    is_within_contact_window,
    validate_facts,
)
from reclaim.policy.rules import GOVERNED_ACTION_TYPES, MINIMAL_RULE_SET
from reclaim.policy.templates import TEMPLATE_REGISTRY, template_for

TS = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)  # 14:30 IST
AMOUNT = Money.from_rupees(1499)


def _obligation(**over) -> Obligation:
    fields = dict(
        obligation_id="obl_1",
        kind=ObligationKind.SUBSCRIPTION_INVOICE,
        payer_id="payer_1",
        gross_amount=AMOUNT,
        issued_at=TS - timedelta(days=30),
        due_at=TS - timedelta(days=2),
        status=ObligationStatus.OPEN,
    )
    fields.update(over)
    return Obligation(**fields)


def _case(**over) -> RiskCase:
    amount = over.pop("amount_at_risk", AMOUNT)
    segment = over.pop("segment", Segment.B2C_STANDARD)
    decline = over.pop("canonical_decline_class", DeclineClass.INSUFFICIENT_FUNDS)
    risk_class = over.pop("risk_class", RiskClass.FAILED_RECURRING_DEBIT)
    failure_class = decline if decline is not None else risk_class
    fields = dict(
        case_id="case_1",
        obligation_id="obl_1",
        payer_id="payer_1",
        risk_class=risk_class,
        segment=segment,
        canonical_decline_class=decline,
        amount_at_risk=amount,
        detected_at=TS,
        stratum=StratumKey.build(
            amount=amount, failure_class=failure_class, segment=segment
        ),
        arm=Arm.A3,
        state=CaseState.DETECTED,
        recovery_window_ends_at=TS + timedelta(days=21),
    )
    fields.update(over)
    return RiskCase(**fields)


def _consent(**over) -> ConsentProfile:
    fields = dict(
        payer_id="payer_1",
        records=(
            ConsentRecord(
                channel=Channel.WHATSAPP,
                granted=True,
                granted_at=TS - timedelta(days=90),
                source="signup_form_v3",
                dpdp_purpose="payment_recovery",
            ),
        ),
        language=Language.EN_IN,
    )
    fields.update(over)
    return ConsentProfile(**fields)


def _context(**over) -> FactContext:
    fields = dict(
        case=_case(),
        obligation=_obligation(),
        now=TS,
        thresholds=PolicyThresholds(),
        consent=_consent(),
    )
    fields.update(over)
    return FactContext(**fields)


def _debit_envelope(**over) -> ActionEnvelope:
    return ActionEnvelope(
        action_id="act_1",
        case_id="case_1",
        action=ScheduleDebit(
            obligation_id="obl_1",
            rail=over.pop("rail", Rail.CARD_ONE_TIME),
            amount=over.pop("amount", AMOUNT),
            execute_at=TS + timedelta(days=1),
            attempt_sequence=over.pop("attempt_sequence", 2),
            mandate_id=over.pop("mandate_id", None),
            # An e-mandate rail cannot even be *expressed* without pointing at the
            # notification that preceded it (invariant #4's structural half), so a
            # mandate-rail fixture has to carry one.
            pre_debit_notification_id=over.pop("pre_debit_notification_id", None),
        ),
        proposed_by=PlanOrigin.DETERMINISTIC_ROUTER,
    )


def _message_envelope(**over) -> ActionEnvelope:
    intent = over.pop("intent", MessageIntent.PAYMENT_REMINDER)
    language = over.pop("language", Language.EN_IN)
    template = template_for(intent, language)
    return ActionEnvelope(
        action_id="act_1",
        case_id="case_1",
        action=SendMessage(
            channel=over.pop("channel", Channel.WHATSAPP),
            template_id=over.pop(
                "template_id", template.template_id if template else "tpl_unknown"
            ),
            language=language,
            intent=intent,
            slots=over.pop("slots", ()),
        ),
        proposed_by=PlanOrigin.DETERMINISTIC_ROUTER,
    )


# ------------------------------------------------------------- the contract


def test_the_bundle_covers_every_fact_the_governed_rules_reference():
    """The property that keeps ``unevaluable`` from becoming the normal case.

    A rule referencing a fact the builder cannot supply is not a rule that
    over-blocks -- it is a rule that never runs and a category that goes silent.
    """
    envelopes = {
        ActionType.SCHEDULE_DEBIT: _debit_envelope(),
        ActionType.SEND_MESSAGE: _message_envelope(),
    }
    for action_type in GOVERNED_ACTION_TYPES:
        envelope = envelopes[action_type]
        facts = build_facts(envelope, _context())
        referenced: set[F] = set()
        for rule in applicable_rules(
            MINIMAL_RULE_SET,
            action_type,
            segment=Segment.B2C_STANDARD,
            channel=Channel.WHATSAPP,
        ):
            referenced |= set(rule.referenced_facts)
            if rule.defer_until_fact is not None:
                referenced.add(rule.defer_until_fact)
        assert referenced <= set(facts), (
            f"{action_type.value} is missing facts: "
            f"{sorted(f.value for f in referenced - set(facts))}"
        )


def test_every_built_bundle_type_checks_against_the_frozen_fact_types():
    for envelope in (_debit_envelope(), _message_envelope()):
        validate_facts(build_facts(envelope, _context()))


def test_validate_facts_rejects_an_enum_member_where_a_string_is_declared():
    """``rail eq "card_one_time"`` against a ``Rail`` member reads correctly and
    is the exact shape of CONTRACTS.md N2."""
    with pytest.raises(ValueError, match="rail"):
        validate_facts({F.RAIL: Rail.CARD_ONE_TIME.value[:4]})


def test_validate_facts_rejects_a_bare_number_for_money():
    with pytest.raises(ValueError, match="debit_amount"):
        validate_facts({F.DEBIT_AMOUNT: 149900})


def test_validate_facts_rejects_a_naive_timestamp():
    with pytest.raises(ValueError, match="quiet_hours_end_at"):
        validate_facts({F.QUIET_HOURS_END_AT: datetime(2026, 8, 1, 9, 0, 0)})


def test_validate_facts_rejects_a_bool_where_a_count_is_declared():
    with pytest.raises(ValueError, match="contacts_total_this_case"):
        validate_facts({F.CONTACTS_TOTAL_THIS_CASE: True})


# ------------------------------------------------------------- quiet hours


def test_the_payers_own_window_governs_in_their_own_zone():
    """JC-43: a stated preference outranks the configured default, and it is read
    in the zone it was stated in. 09:00 UTC is 04:00 in New York -- outside a
    09:00-19:00 window there, and inside the same window in IST."""
    new_york = QuietHours(
        start_hour_local=9, end_hour_local=19, timezone_name="America/New_York"
    )
    kolkata = QuietHours(
        start_hour_local=9, end_hour_local=19, timezone_name="Asia/Kolkata"
    )
    assert is_within_contact_window(TS, new_york) is False
    assert is_within_contact_window(TS, kolkata) is True


def test_the_configured_window_is_read_in_the_fallback_zone():
    """No stated window means the configured one, read as Asia/Kolkata -- not as
    whatever zone the server happens to be deployed in."""
    facts = build_facts(
        _message_envelope(), _context(consent=_consent(quiet_hours=None))
    )
    assert facts[F.IS_WITHIN_QUIET_HOURS] is True  # 14:30 IST
    late = build_facts(
        _message_envelope(),
        _context(consent=_consent(quiet_hours=None), now=TS + timedelta(hours=6)),
    )
    assert late[F.IS_WITHIN_QUIET_HOURS] is False  # 20:30 IST
    assert FALLBACK_QUIET_HOURS_TIMEZONE == "Asia/Kolkata"


def test_a_stated_window_beats_the_configured_one_through_the_builder():
    narrow = QuietHours(
        start_hour_local=9, end_hour_local=12, timezone_name="Asia/Kolkata"
    )
    facts = build_facts(
        _message_envelope(), _context(consent=_consent(quiet_hours=narrow))
    )
    assert facts[F.IS_WITHIN_QUIET_HOURS] is False  # 14:30 IST, window closed at 12


def test_the_window_end_is_the_next_close_in_the_payers_zone():
    window = QuietHours(
        start_hour_local=9, end_hour_local=19, timezone_name="Asia/Kolkata"
    )
    end = contact_window_end(TS, window)  # 14:30 IST -> 19:00 IST today
    assert end == datetime(2026, 8, 1, 13, 30, tzinfo=timezone.utc)
    after_close = contact_window_end(TS + timedelta(hours=6), window)  # 20:30 IST
    assert after_close == datetime(2026, 8, 2, 13, 30, tzinfo=timezone.utc)


# ------------------------------------------------------------ derived facts


def test_a_debit_carries_its_amount_as_money():
    facts = build_facts(_debit_envelope(amount=Money.from_rupees(75000)), _context())
    assert facts[F.DEBIT_AMOUNT] == Money.from_rupees(75000)


def test_a_rail_with_no_mandate_is_debitable_and_a_mandate_backed_one_is_not():
    """CARD_ONE_TIME is not mandate-backed -- there is no mandate to invalidate.
    An e-mandate rail with no mandate record fails closed (§10.1)."""
    card = build_facts(_debit_envelope(rail=Rail.CARD_ONE_TIME), _context())
    assert card[F.MANDATE_IS_DEBITABLE] is True

    emandate = build_facts(
        _debit_envelope(
            rail=Rail.CARD_EMANDATE,
            mandate_id="mnd_1",
            pre_debit_notification_id="ntf_1",
        ),
        _context(mandate=None),
    )
    assert emandate[F.MANDATE_IS_DEBITABLE] is False


def test_a_paused_mandate_is_not_debitable():
    paused = Mandate(
        mandate_id="mnd_1",
        payer_id="payer_1",
        psp=PspId.STRIPE_TEST,
        rail=Rail.CARD_EMANDATE,
        status=MandateStatus.PAUSED,
        cap=Money.from_rupees(50000),
    )
    facts = build_facts(
        _debit_envelope(
            rail=Rail.CARD_EMANDATE,
            mandate_id="mnd_1",
            pre_debit_notification_id="ntf_1",
        ),
        _context(mandate=paused),
    )
    assert facts[F.MANDATE_IS_DEBITABLE] is False


def test_hardness_comes_from_the_frozen_taxonomy_not_from_a_local_list():
    soft = build_facts(
        _debit_envelope(),
        _context(case=_case(canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS)),
    )
    hard = build_facts(
        _debit_envelope(),
        _context(case=_case(canonical_decline_class=DeclineClass.CARD_EXPIRED)),
    )
    assert soft[F.LAST_DECLINE_WAS_HARD] is False
    assert hard[F.LAST_DECLINE_WAS_HARD] is True


def test_a_settled_obligation_is_visible_to_the_integrity_gate():
    facts = build_facts(
        _debit_envelope(),
        _context(obligation=_obligation(status=ObligationStatus.PAID)),
    )
    assert facts[F.OBLIGATION_ALREADY_SETTLED] is True


def test_a_reused_idempotency_key_is_visible_to_the_integrity_gate():
    envelope = _debit_envelope()
    facts = build_facts(
        envelope, _context(used_idempotency_keys=frozenset({envelope.idempotency_key}))
    )
    assert facts[F.IDEMPOTENCY_KEY_ALREADY_USED] is True


# ---------------------------------------------------------------- consent


def test_an_absent_consent_profile_denies_rather_than_abstains():
    facts = build_facts(_message_envelope(), _context(consent=None))
    assert facts[F.CONSENT_RECORD_EXISTS] is False
    assert facts[F.HAS_CHANNEL_CONSENT] is False


def test_consent_is_per_channel():
    facts = build_facts(_message_envelope(channel=Channel.EMAIL), _context())
    assert facts[F.CONSENT_RECORD_EXISTS] is False  # consented on WhatsApp only
    assert facts[F.HAS_CHANNEL_CONSENT] is False


def test_a_withdrawn_record_reads_as_opted_out():
    withdrawn = _consent(
        records=(
            ConsentRecord(
                channel=Channel.WHATSAPP,
                granted=True,
                granted_at=TS - timedelta(days=90),
                withdrawn_at=TS - timedelta(days=1),
                source="stop_reply",
                dpdp_purpose="payment_recovery",
            ),
        )
    )
    facts = build_facts(_message_envelope(), _context(consent=withdrawn))
    assert facts[F.IS_OPTED_OUT] is True
    assert facts[F.HAS_CHANNEL_CONSENT] is False


def test_the_dpdp_purpose_must_cover_the_message_intent():
    marketing_only = _consent(
        records=(
            ConsentRecord(
                channel=Channel.WHATSAPP,
                granted=True,
                granted_at=TS - timedelta(days=90),
                source="signup_form_v3",
                dpdp_purpose="marketing",
            ),
        )
    )
    facts = build_facts(_message_envelope(), _context(consent=marketing_only))
    assert facts[F.DPDP_PURPOSE_COVERS_ACTION] is False


def test_the_language_must_match_the_consented_one():
    facts = build_facts(
        _message_envelope(language=Language.HI_IN, template_id="tpl_payment_reminder_v1"),
        _context(),
    )
    assert facts[F.CONSENT_LANGUAGE_MATCHES] is False


# ------------------------------------------------------------------ holds


def test_an_open_hold_is_visible_and_a_released_one_is_not():
    open_hold = Hold(
        hold_id="hold_1",
        payer_id="payer_1",
        kind=StopReason.HARD_STOP_DISPUTE,
        opened_at=TS - timedelta(days=1),
        reason="chargeback filed",
        opened_by="ops",
    )
    released = Hold(
        hold_id="hold_2",
        payer_id="payer_1",
        kind=StopReason.HARD_STOP_DISPUTE,
        opened_at=TS - timedelta(days=10),
        released_at=TS - timedelta(days=5),
        reason="resolved",
        opened_by="ops",
    )
    live = build_facts(_debit_envelope(), _context(holds=(open_hold,)))
    closed = build_facts(_debit_envelope(), _context(holds=(released,)))
    assert live[F.HAS_OPEN_DISPUTE] is True and live[F.HAS_ACTIVE_HOLD] is True
    assert closed[F.HAS_OPEN_DISPUTE] is False and closed[F.HAS_ACTIVE_HOLD] is False


def test_a_hold_with_no_dedicated_fact_still_registers_as_an_active_hold():
    """Bereavement is one of §14.1's seven hard stops and has no fact of its own.
    ``has_active_hold`` is what stops it being invisible to the engine."""
    bereavement = Hold(
        hold_id="hold_3",
        payer_id="payer_1",
        kind=StopReason.HARD_STOP_BEREAVEMENT,
        opened_at=TS - timedelta(days=1),
        reason="notified by family",
        opened_by="support",
    )
    facts = build_facts(_debit_envelope(), _context(holds=(bereavement,)))
    assert facts[F.HAS_ACTIVE_HOLD] is True


# --------------------------------------------------------------- templates


def test_an_unregistered_template_fails_the_content_gate_closed():
    facts = build_facts(_message_envelope(template_id="tpl_not_registered"), _context())
    assert facts[F.TEMPLATE_IS_DLT_REGISTERED] is False


def test_a_registered_template_with_named_slots_passes_content():
    facts = build_facts(_message_envelope(), _context())
    assert facts[F.TEMPLATE_IS_DLT_REGISTERED] is True
    assert facts[F.HAS_FREE_TEXT_SLOT] is False
    assert facts[F.CONTAINS_BANNED_PHRASE] is False


def test_a_banned_phrase_in_a_slot_value_is_detected():
    facts = build_facts(
        _message_envelope(
            slots=(
                TemplateSlotValue(
                    name="amount_due", value="Pay now or we will report you to CIBIL"
                ),
            )
        ),
        _context(),
    )
    assert facts[F.CONTAINS_BANNED_PHRASE] is True


def test_every_registered_template_declares_an_intent_it_can_serve():
    for template_id, spec in TEMPLATE_REGISTRY.items():
        assert spec.template_id == template_id
        assert spec.languages
        assert template_for(spec.intent, next(iter(spec.languages))) is not None
