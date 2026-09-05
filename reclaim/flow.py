"""The wiring: seeded ledger -> diagnosis -> policy -> outbox -> state -> audit.

This is the first module in the repository that makes the pieces touch. Nothing
here is new capability; it is the order the existing parts run in, the small
deterministic router that decides *which* verb to propose, and the two stand-ins
that stand where a store does not exist yet.

The path, per case
------------------
1. **Arm gate.** ``ARM_SPECS`` decides whether this case may be acted on at all.
   A0 takes no action (§12.2's natural-recovery floor); A1/A2 run a fixed
   baseline ladder that Phase 1 has not built; A5 runs with the policy engine
   *off* and is simulation-only (JC-08), so deciding for it here would be the one
   place a real rail could be reached without a verdict. Only A3 and A4 --
   the arms whose ``ArmSpec`` sets ``deterministic_routing_enabled`` and keeps
   the policy engine on -- route.
2. **Diagnose.** ``reclaim.diagnosis.deterministic.diagnose`` -- table lookup, no
   LLM. Audited as ``diagnosis_produced``.
3. **Confidence gate.** Below ``PolicyThresholds.diagnosis_confidence_floor`` the
   case escalates to a human. That is not the policy engine tiering up; it is the
   diagnostician's own contract being honoured: it reports a contested dispatch
   *below* the floor precisely so nothing downstream auto-acts on a coin flip
   between opposite interventions.
4. **Route.** Root cause -> one proposed ``ActionEnvelope``, or nothing.
5. **Evaluate.** Facts from the ledger, the obligation, the outbox and the clock;
   ``policy.engine.evaluate`` returns the verdict. Audited as ``policy_evaluated``
   whatever it says -- §14.1 logs allows too.
6. **Act, hold, or stop.** ALLOW enqueues to the outbox and walks the case
   DIAGNOSING -> PLANNED -> SCHEDULED. ALLOW_WITH_APPROVAL walks it
   DIAGNOSING -> PLANNED -> AWAITING_APPROVAL and enqueues **nothing** -- it
   waits on ``ledger.list_awaiting_approval`` for a human. DENY (and a
   fail-closed no-verdict) stops it with ``POLICY_BLOCKED``.

Time is derived, never read
---------------------------
Every case is evaluated at ``detected_at + PLANNING_LATENCY``. A run that called
``utcnow()`` would decide quiet hours differently depending on when it ran --
green in the afternoon, red overnight, and irreproducible for a judge either way.
The cost is that this is a replay of a seeded past, not a live scheduler; a real
scheduler passes the real clock into the same functions.

Two stand-ins, named as such
----------------------------
``stand_in_consent_profile`` and ``stand_in_holds`` derive a consent record and a
hold list deterministically from the payer id, because the spine has **no consent
table and no holds table**. They are the only inputs to this flow that are not
real seeded data, and they are the reason the consent, timing and holds gates
have anything to bite on. Everything else -- amounts, segments, arms, decline
classes, detection times -- comes from ``reclaim.spine.seed.generate``.

What this flow is not
---------------------
There is no scheduler, so a DENY is terminal even when the honest answer is
"defer until 09:00 in the payer's zone" -- the engine can express DEFER and the
rule set deliberately does not use it, because there is nowhere to put a deferred
action. There is no executor: an allowed action reaches the outbox and stops
there. And there is no approval *consumer*: an AFA-threshold debit now parks in
AWAITING_APPROVAL (§14.2 T2) instead of being refused, but the approve / edit /
reject / SLA-breach edges out of that state need the console (§18.1 item 14), so
a parked case is where ``run`` leaves it -- the same shape of dead end as a
quiet-hours DENY. The rest of §14.2's deterministic tier resolver is still
unbuilt (``policy.rules.NOT_YET_ENCODED``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from reclaim.contracts.actions import (
    ActionEnvelope,
    ActionType,
    ScheduleDebit,
    SendMessage,
    TemplateSlotValue,
)
from reclaim.contracts.canonical import digest
from reclaim.contracts.case import Diagnosis, RiskCase
from reclaim.contracts.enums import (
    ARM_SPECS,
    ActorType,
    Arm,
    AutonomyTier,
    CaseState,
    Channel,
    Language,
    MessageIntent,
    PlanOrigin,
    PolicyEffect,
    Rail,
    RootCauseClass,
    Segment,
    StopReason,
)
from reclaim.contracts.money import Money
from reclaim.contracts.obligations import (
    ConsentProfile,
    ConsentRecord,
    Hold,
    Obligation,
    QuietHours,
)
from reclaim.contracts.policy_format import PolicyThresholds
from reclaim.contracts.temporal import to_rfc3339
from reclaim.diagnosis.deterministic import diagnose
from reclaim.policy.engine import evaluate
from reclaim.policy.facts import FactContext, build_facts, validate_facts
from reclaim.policy.rules import (
    GOVERNED_ACTION_TYPES,
    MINIMAL_RULE_SET,
    build_minimal_rule_set,
)
from reclaim.policy.templates import template_for
from reclaim.spine import audit_store, case_machine, ledger, outbox
from reclaim.spine.tables import outbox as outbox_table

__all__ = [
    "CaseResult",
    "DEFAULT_RULE_SET",
    "MERCHANT_NAME",
    "Outcome",
    "PLANNING_LATENCY",
    "process_case",
    "route",
    "run",
    "stand_in_consent_profile",
    "stand_in_holds",
]

#: How long after detection the agent gets around to planning. A constant rather
#: than a clock read: see the module docstring.
PLANNING_LATENCY: timedelta = timedelta(minutes=5)

DEFAULT_RULE_SET = MINIMAL_RULE_SET

MERCHANT_NAME = "RECLAIM Demo Merchant"

#: The arms this flow will act for: deterministic routing on, policy engine on.
#: Derived from ``ARM_SPECS`` rather than listed, so an arm whose spec changes
#: does not silently keep or lose the right to act.
ROUTING_ARMS: frozenset[Arm] = frozenset(
    arm
    for arm, spec in ARM_SPECS.items()
    if spec.deterministic_routing_enabled and spec.policy_engine_enabled
)


class Outcome(StrEnum):
    """What happened to one case in one pass of the flow."""

    CONTROL_ARM_NO_ACTION = "control_arm_no_action"
    BASELINE_LADDER_NOT_IMPLEMENTED = "baseline_ladder_not_implemented"
    SIMULATION_ONLY_ARM_SKIPPED = "simulation_only_arm_skipped"
    ROUTED_TO_HUMAN_LOW_CONFIDENCE = "routed_to_human_low_confidence"
    ROUTED_TO_HUMAN_NO_ROUTE = "routed_to_human_no_route"
    ALLOWED = "allowed"
    PENDING_APPROVAL = "pending_approval"
    DENIED = "denied"


@dataclass(frozen=True)
class CaseResult:
    """One case's trip through the flow, flat enough to print or tabulate."""

    case_id: str
    arm: Arm
    segment: Segment
    amount_at_risk: Money
    risk_class: str
    decline_class: str | None
    evaluated_at: datetime
    outcome: Outcome
    root_cause: RootCauseClass | None = None
    confidence: Decimal | None = None
    action_type: ActionType | None = None
    channel: Channel | None = None
    effect: PolicyEffect | None = None
    deciding_rule_id: str | None = None
    requires_tier: AutonomyTier | None = None
    reason: str = ""
    outbox_id: int | None = None
    final_state: CaseState = CaseState.DETECTED
    hedged: bool = False
    hedged_intent: MessageIntent | None = None

    @property
    def is_allowed(self) -> bool:
        return self.outcome is Outcome.ALLOWED

    @property
    def is_pending_approval(self) -> bool:
        return self.outcome is Outcome.PENDING_APPROVAL


# ---------------------------------------------------------------------------
# Stand-ins for stores that do not exist yet
# ---------------------------------------------------------------------------


def _payer_ordinal(payer_id: str) -> int:
    """The numeric tail of a seeded payer id, or a stable fallback.

    Only ever used to *vary* the stand-ins below. Nothing real keys off it.
    """
    tail = payer_id.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else abs(hash(tail)) % 97


def stand_in_consent_profile(payer_id: str) -> ConsentProfile:
    """A deterministic consent profile. **Not a consent store.**

    The spine has no consent table, and §14.1's consent gate is meaningless
    against an empty one: every contact would be denied for the same reason and
    the timing and content gates would never be reached. So this derives a varied
    profile from the payer id -- some opted out, some on the DNC list, some
    consented for a different DPDP purpose, some with a stated contact window in
    a zone that is not the fallback.

    Everything here is fabricated. A real consent record carries provenance
    (``source``), a capture time and a purpose that a human agreed to; these
    carry plausible-looking values so the gates have something to read. When the
    consent store lands, this function is deleted, not extended.
    """
    n = _payer_ordinal(payer_id)
    captured = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    purpose = "marketing" if n % 9 == 0 else "payment_recovery"
    withdrawn = captured + timedelta(days=60) if n % 7 == 0 else None

    records = tuple(
        ConsentRecord(
            channel=channel,
            granted=True,
            granted_at=captured,
            withdrawn_at=withdrawn,
            source="stand_in_seed_profile",
            dpdp_purpose=purpose,
        )
        for channel in (Channel.WHATSAPP, Channel.EMAIL)
    )

    if n % 5 == 0:
        quiet_hours = QuietHours(
            start_hour_local=9, end_hour_local=12, timezone_name="Asia/Kolkata"
        )
    elif n % 13 == 0:
        quiet_hours = QuietHours(
            start_hour_local=9, end_hour_local=19, timezone_name="America/New_York"
        )
    else:
        quiet_hours = None

    return ConsentProfile(
        payer_id=payer_id,
        records=records,
        language=Language.EN_IN,
        quiet_hours=quiet_hours,
        on_dnc_list=n % 11 == 0,
        updated_at=captured,
    )


def stand_in_holds(case: RiskCase) -> tuple[Hold, ...]:
    """A deterministic hold list. **Not a holds store.**

    Same reasoning as the consent profile: §14.1's Holds row is the hardest stop
    in the system and would never fire against an empty table.
    """
    n = _payer_ordinal(case.payer_id)
    if n % 17 != 0:
        return ()
    return (
        Hold(
            hold_id=f"hold_{n:04d}",
            payer_id=case.payer_id,
            obligation_id=case.obligation_id,
            kind=StopReason.HARD_STOP_DISPUTE,
            opened_at=case.detected_at - timedelta(days=1),
            reason="stand-in: payer disputed this charge before detection",
            opened_by="stand_in_seed_profile",
        )
    ,)


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------


def _contact_channel(segment: Segment) -> Channel:
    """WhatsApp for consumers, email for businesses.

    A placeholder for the channel-selection model §11 describes, and stated as
    one: nothing here reads engagement history, because nothing records it.
    """
    return Channel.EMAIL if segment.value.startswith("b2b") else Channel.WHATSAPP


def _slot_values(
    template_slots: Iterable[str], case: RiskCase, obligation: Obligation
) -> tuple[TemplateSlotValue, ...]:
    """Fill a registered template's named slots. No slot is free text (JC-17)."""
    available: Mapping[str, str] = {
        "amount_due": str(case.amount_at_risk),
        "merchant_name": MERCHANT_NAME,
        "due_date": obligation.due_at.date().isoformat(),
        "update_link": f"https://pay.example.test/update/{case.case_id}",
        "afa_link": f"https://pay.example.test/afa/{case.case_id}",
        "reauth_link": f"https://pay.example.test/mandate/{case.case_id}",
    }
    return tuple(
        TemplateSlotValue(name=name, value=available[name])
        for name in sorted(template_slots)
        if name in available
    )


def _message(
    case: RiskCase,
    obligation: Obligation,
    intent: MessageIntent,
    *,
    language: Language = Language.EN_IN,
) -> ActionEnvelope | None:
    """Propose a templated contact, or nothing if no template is registered."""
    template = template_for(intent, language)
    if template is None:
        return None
    return ActionEnvelope(
        action_id=f"act_{case.case_id.split('_', 1)[-1]}_msg",
        case_id=case.case_id,
        action=SendMessage(
            channel=_contact_channel(case.segment),
            template_id=template.template_id,
            language=language,
            intent=intent,
            slots=_slot_values(template.slot_names, case, obligation),
        ),
        proposed_by=PlanOrigin.DETERMINISTIC_ROUTER,
    )


def _retry_debit(
    case: RiskCase, obligation: Obligation, *, at: datetime
) -> ActionEnvelope | None:
    """Re-present a soft-declined debit on the rail the obligation was raised on.

    ``CARD_ONE_TIME`` is the only rail the seed generator uses, and it is neither
    mandate-backed nor subject to a pre-debit notification, so a retry is
    expressible without a mandate reference. A mandate rail would need both, and
    the ``ScheduleDebit`` validator would refuse without them -- which is
    invariant #4's structural half doing its job rather than a gap here.
    """
    return ActionEnvelope(
        action_id=f"act_{case.case_id.split('_', 1)[-1]}_dbt",
        case_id=case.case_id,
        action=ScheduleDebit(
            obligation_id=case.obligation_id,
            rail=Rail.CARD_ONE_TIME,
            amount=case.amount_at_risk,
            execute_at=at + timedelta(hours=24),
            attempt_sequence=2,
        ),
        proposed_by=PlanOrigin.DETERMINISTIC_ROUTER,
    )


#: Root cause -> the one verb this router proposes for it. §9.2's intervention
#: column, reduced to the two verbs the minimal rule set governs. Everything
#: absent from this table routes to a human, which is the whole reason it is a
#: table: a root cause nobody has decided an action for must not fall through to
#: the nearest plausible one.
_ROUTES: Mapping[RootCauseClass, Any] = {
    RootCauseClass.H1_TIMING_LIQUIDITY: "retry_debit",
    RootCauseClass.H2_CREDENTIAL_LIFECYCLE: MessageIntent.CREDENTIAL_UPDATE_REQUEST,
    RootCauseClass.H3_MANDATE_DEAD_OR_PAUSED: MessageIntent.MANDATE_REAUTH_REQUEST,
    RootCauseClass.H4_AFA_STEP_UP_INCOMPLETE: MessageIntent.AFA_COMPLETION_REQUEST,
    RootCauseClass.H9_B2B_LIQUIDITY_OR_WILLFUL_DELAY: MessageIntent.PAYMENT_REMINDER,
}


def route(
    case: RiskCase,
    obligation: Obligation,
    diagnosis: Diagnosis,
    *,
    at: datetime,
) -> ActionEnvelope | None:
    """The one action this root cause calls for, or ``None`` for a human.

    Deliberately not a planner: one root cause, one verb, no sequencing, no
    timing model, no value gate. Arms A3 and A4 differ in this repo only by which
    diagnosis feeds it -- and today only the deterministic one exists, so they run
    identically. That is a stated limitation of the wiring, not of the design:
    §12.1's ablation needs the LLM path before A4 means anything.
    """
    if diagnosis.root_cause.forbids_payment_nudge:
        # §9.2: for churn intent and a commercial dispute the payment nudge is the
        # wrong action, and the right one (retention, dispute handling) is not in
        # this router's two verbs. Refusing beats guessing.
        return None
    target = _ROUTES.get(diagnosis.root_cause)
    if target is None:
        return None
    if target == "retry_debit":
        return _retry_debit(case, obligation, at=at)
    return _message(case, obligation, target)


#: The contact a *contested* dispatch is allowed to send when a debit is known to
#: have failed. Chosen because it is the one registered intent that is true under
#: every candidate the taxonomy files for an ambiguous decline: it reports the
#: failure rather than asking for money, and carries both the completion door and
#: the change-how-you-pay door, so H4 (finish the AFA), H3 (re-authorise) and H5
#: (stop paying, deliberately) each have a one-tap answer.
HEDGED_DECLINE_INTENT: MessageIntent = MessageIntent.PAYMENT_FAILED_INFORM

#: The same, for a case where **no debit was ever attempted** -- an overdue
#: receivable. ``PAYMENT_FAILED_INFORM`` would be a false statement there, and a
#: template that lies is worse than an escalation. See ``hedged_route`` for why
#: this one is a weaker guarantee than the decline hedge.
HEDGED_RECEIVABLE_INTENT: MessageIntent = MessageIntent.PAYMENT_REMINDER


def hedged_route(
    case: RiskCase,
    obligation: Obligation,
    diagnosis: Diagnosis,
    *,
    at: datetime,
) -> ActionEnvelope | None:
    """The one contact a below-floor diagnosis may still send, or ``None``.

    Why this exists at all
    ----------------------
    The confidence floor was answering the right question -- "should a contested
    dispatch auto-act on a coin flip between opposite interventions?" -- with an
    action, ``ESCALATED``, that Phase 1 has no consumer for. So the case did not
    tier up; it stopped. Measured on the n=200 seed that was **25 of arm A4's 48
    cases**, and it is the single reason A4 loses to A1's undifferentiated drip:
    A1 contacts everyone at a modest uplift, A4 contacted 19% at a good one.

    What it does **not** do
    -----------------------
    It does not lower the floor, and it does not raise the confidence. The
    diagnosis is still reported contested, the case is still recorded as such
    (``contested_diagnosis_hedged``), and the policy engine still evaluates the
    proposal like any other. Two things are refused outright:

    * **an abstention.** ``root_cause=UNKNOWN`` is not a contested dispatch, it is
      a blank one -- there is no hypothesis for a hedge to be true under, and the
      H6 abstention (§10.1's most damaging wrong call) arrives here. Nothing is
      sent.
    * **a debit.** §9.2 H3: re-presenting a dead mandate recovers 0% and still
      costs a failed-attempt fee, and a contested dispatch is precisely the state
      where H3 cannot be ruled out. A hedge is a contact or it is nothing.

    The two hedges are not equally strong, and the difference is deliberate
    ---------------------------------------------------------------------------
    * **A failed debit (D1).** The candidates genuinely oppose -- H4 wants the AFA
      finished, H5 wants to be left alone -- so the hedge is *not* the verb the
      router would have chosen above the floor. It is a strictly weaker,
      non-committal notification, and ``sim.anchors`` prices it below a correctly
      targeted contact because that is what it is.
    * **An overdue receivable (D3).** Here the hedge *is* what H9 would have
      routed above the floor, so for this branch the fallback is equivalent to
      admitting that filing H8 as an alternative never made the dispatch contested
      in the first place -- H9 (chase) and H8 (our invoice is wrong) do not demand
      opposite contacts; a reminder is how a defective invoice gets discovered.
      That is a defensible outcome reached by the wrong route: the honest fix is
      in the diagnostician's rung selection, not here. Recorded rather than
      hidden, and it is why the two branches are counted separately in any report.
    """
    if diagnosis.root_cause is RootCauseClass.UNKNOWN:
        return None
    if not diagnosis.alternative_root_causes:
        # Below the floor without alternatives means something other than a
        # contested dispatch put it there -- a lowered threshold, or a future
        # calibrated diagnosis that is simply unsure. Neither is this rule's
        # business, and guessing an action for it is how a hedge becomes a
        # general-purpose floor bypass.
        return None
    if case.canonical_decline_class is not None:
        return _message(case, obligation, HEDGED_DECLINE_INTENT)
    candidates = (diagnosis.root_cause, *diagnosis.alternative_root_causes)
    if any(cause.forbids_payment_nudge for cause in candidates):
        # No debit failed, so the only truthful contact left is a reminder -- and
        # a reminder is a payment nudge. If any live hypothesis forbids one there
        # is nothing honest to send.
        return None
    return _message(case, obligation, HEDGED_RECEIVABLE_INTENT)


# ---------------------------------------------------------------------------
# One case
# ---------------------------------------------------------------------------


def _for_digest(value: Any) -> Any:
    """A fact value in a shape ``canonical_json`` accepts (no floats, ever)."""
    if isinstance(value, Money):
        return {"paise": value.paise, "currency": value.currency.value}
    if isinstance(value, datetime):
        return to_rfc3339(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def _outbox_counts(conn: Connection, case: RiskCase) -> tuple[int, int]:
    """(debit attempts on this obligation, contacts on this case) from the outbox.

    The second is used for *both* frequency facts. The outbox has no channel
    column and, in this flow, a case contacts on exactly one channel -- so the
    per-channel count and the per-case count are the same number. That equality
    stops holding the moment a case uses two channels, and the fix is a column,
    not a cleverer query. Flagged the same way ``metrics.contacted_case_count``
    is (CONTRACTS.md Q7): an approximation that is written down.
    """
    debits = conn.execute(
        sa.select(sa.func.count())
        .select_from(outbox_table)
        .where(
            outbox_table.c.obligation_id == case.obligation_id,
            outbox_table.c.action_type == ActionType.SCHEDULE_DEBIT.value,
        )
    ).scalar_one()
    contacts = conn.execute(
        sa.select(sa.func.count())
        .select_from(outbox_table)
        .where(
            outbox_table.c.case_id == case.case_id,
            outbox_table.c.action_type == ActionType.SEND_MESSAGE.value,
        )
    ).scalar_one()
    return int(debits), int(contacts)


def _key_already_used(conn: Connection, key: str) -> frozenset[str]:
    found = conn.execute(
        sa.select(outbox_table.c.idempotency_key).where(
            outbox_table.c.idempotency_key == key
        )
    ).first()
    return frozenset({key}) if found is not None else frozenset()


def _skip(case: RiskCase, outcome: Outcome, at: datetime, reason: str) -> CaseResult:
    return CaseResult(
        case_id=case.case_id,
        arm=case.arm,
        segment=case.segment,
        amount_at_risk=case.amount_at_risk,
        risk_class=case.risk_class.value,
        decline_class=(
            case.canonical_decline_class.value
            if case.canonical_decline_class is not None
            else None
        ),
        evaluated_at=at,
        outcome=outcome,
        reason=reason,
        final_state=case.state,
    )


def process_case(
    conn: Connection,
    case: RiskCase,
    *,
    rule_set=DEFAULT_RULE_SET,
    thresholds: PolicyThresholds | None = None,
) -> CaseResult:
    """Run one case from its detected state to a verdict and its consequence."""
    resolved = thresholds if thresholds is not None else PolicyThresholds()
    at = case.detected_at + PLANNING_LATENCY

    arm_spec = ARM_SPECS[case.arm]
    if not arm_spec.takes_any_action:
        audit_store.append(
            conn,
            ts=at,
            case_id=case.case_id,
            actor=ActorType.SYSTEM,
            event_type="control_arm_no_action",
            inputs_digest=digest({"case_id": case.case_id, "arm": case.arm.value}),
            decision_rationale=(
                "arm A0 is the no-action control (§12.2); the case is observed and "
                "left in detected so the natural-recovery floor is not contaminated"
            ),
        )
        return _skip(
            case,
            Outcome.CONTROL_ARM_NO_ACTION,
            at,
            "control arm: no action by construction",
        )

    if case.arm not in ROUTING_ARMS:
        reason = (
            "policy engine is disabled for this arm (JC-08: simulation only)"
            if not arm_spec.policy_engine_enabled
            else "fixed baseline ladder is not implemented in Phase 1"
        )
        outcome = (
            Outcome.SIMULATION_ONLY_ARM_SKIPPED
            if not arm_spec.policy_engine_enabled
            else Outcome.BASELINE_LADDER_NOT_IMPLEMENTED
        )
        audit_store.append(
            conn,
            ts=at,
            case_id=case.case_id,
            actor=ActorType.SYSTEM,
            event_type="arm_not_routed",
            inputs_digest=digest({"case_id": case.case_id, "arm": case.arm.value}),
            decision_rationale=reason,
        )
        return _skip(case, outcome, at, reason)

    obligation = ledger.get_obligation(conn, case.obligation_id)
    if obligation is None:  # pragma: no cover - the ledger's FK forbids it
        raise ValueError(f"case {case.case_id} references an unknown obligation")

    diagnosis = diagnose(case, created_at=at)
    audit_store.append(
        conn,
        ts=at,
        case_id=case.case_id,
        actor=ActorType.AGENT,
        event_type="diagnosis_produced",
        inputs_digest=digest(
            {
                "case_id": case.case_id,
                "risk_class": case.risk_class.value,
                "decline_class": (
                    case.canonical_decline_class.value
                    if case.canonical_decline_class is not None
                    else None
                ),
            }
        ),
        decision_rationale=diagnosis.reasoning_summary[:2000],
    )
    case = case_machine.transition(
        conn,
        case.case_id,
        CaseState.DIAGNOSING,
        actor=ActorType.AGENT,
        at=at,
        rationale="deterministic diagnosis started",
    )

    base = dict(
        case_id=case.case_id,
        arm=case.arm,
        segment=case.segment,
        amount_at_risk=case.amount_at_risk,
        risk_class=case.risk_class.value,
        decline_class=(
            case.canonical_decline_class.value
            if case.canonical_decline_class is not None
            else None
        ),
        evaluated_at=at,
        root_cause=diagnosis.root_cause,
        confidence=diagnosis.confidence,
    )

    floor = Decimal(resolved.diagnosis_confidence_floor)
    envelope: ActionEnvelope | None
    if diagnosis.confidence < floor:
        envelope = hedged_route(case, obligation, diagnosis, at=at)
        if envelope is None:
            reason = (
                f"diagnosis confidence {diagnosis.confidence} is below the policy "
                f"floor {floor} and no hedged contact is true under every candidate "
                "cause; §14.2 tiers up rather than acting"
            )
            case = case_machine.transition(
                conn,
                case.case_id,
                CaseState.ESCALATED,
                actor=ActorType.AGENT,
                at=at,
                rationale=reason,
            )
            return CaseResult(
                **base,
                outcome=Outcome.ROUTED_TO_HUMAN_LOW_CONFIDENCE,
                reason=reason,
                final_state=case.state,
            )
        intent = envelope.action.intent
        base["hedged"] = True
        base["hedged_intent"] = intent
        # Written *before* the policy evaluation, and unconditionally: a reader
        # who finds a scheduled contact and no row here is entitled to read it as
        # a confident intervention, so the hedge has to be on the chain whether or
        # not the engine goes on to allow it.
        audit_store.append(
            conn,
            ts=at,
            case_id=case.case_id,
            actor=ActorType.AGENT,
            event_type="contested_diagnosis_hedged",
            inputs_digest=digest(
                {
                    "case_id": case.case_id,
                    "confidence": str(diagnosis.confidence),
                    "floor": str(floor),
                    "root_cause": diagnosis.root_cause.value,
                    "alternatives": [
                        c.value for c in diagnosis.alternative_root_causes
                    ],
                    "hedged_intent": intent.value,
                }
            ),
            decision_rationale=(
                f"confidence {diagnosis.confidence} is below the floor {floor} and "
                f"the dispatch is contested between {diagnosis.root_cause.value} and "
                f"{', '.join(c.value for c in diagnosis.alternative_root_causes)}; "
                f"sending {intent.value}, the least-committal contact true under "
                "every candidate. The diagnosis is unchanged and no debit is taken."
            )[:2000],
        )
    else:
        envelope = route(case, obligation, diagnosis, at=at)
    if envelope is None:
        reason = (
            f"no action is routed for root cause {diagnosis.root_cause.value}; "
            "escalated rather than substituting the nearest plausible verb"
        )
        case = case_machine.transition(
            conn,
            case.case_id,
            CaseState.ESCALATED,
            actor=ActorType.AGENT,
            at=at,
            rationale=reason,
        )
        return CaseResult(
            **base,
            outcome=Outcome.ROUTED_TO_HUMAN_NO_ROUTE,
            reason=reason,
            final_state=case.state,
        )

    if envelope.action.action_type not in GOVERNED_ACTION_TYPES:  # pragma: no cover
        raise ValueError(
            f"the router proposed {envelope.action.action_type.value}, which the "
            "minimal rule set does not govern; it would fail closed"
        )

    debits, contacts = _outbox_counts(conn, case)
    context = FactContext(
        case=case,
        obligation=obligation,
        now=at,
        thresholds=resolved,
        consent=stand_in_consent_profile(case.payer_id),
        holds=stand_in_holds(case),
        debit_attempts_this_window=debits,
        contacts_on_channel_last_7d=contacts,
        contacts_total_this_case=contacts,
        used_idempotency_keys=_key_already_used(conn, envelope.idempotency_key),
    )
    facts = build_facts(envelope, context)
    validate_facts(facts)

    result = evaluate(rule_set, envelope, facts, segment=case.segment)
    channel = getattr(envelope.action, "channel", None)

    audit_store.append(
        conn,
        ts=at,
        case_id=case.case_id,
        actor=ActorType.AGENT,
        event_type="policy_evaluated",
        inputs_digest=digest(
            {
                "case_id": case.case_id,
                "idempotency_key": envelope.idempotency_key,
                "rule_set_digest": rule_set.digest,
                "facts": {k.value: _for_digest(v) for k, v in facts.items()},
            }
        ),
        policy_verdict_rule_ids=tuple(v.rule_id for v in result.decision.verdicts),
        policy_decision=result.decision,
        policy_version=rule_set.policy_version,
        decision_rationale=result.explain()[:2000],
    )

    effect = result.decision.effect
    if effect is PolicyEffect.ALLOW:
        return _allow(conn, case, diagnosis, result, at, base, envelope, channel)
    if effect is PolicyEffect.ALLOW_WITH_APPROVAL:
        return _await_approval(
            conn, case, diagnosis, result, at, base, envelope, channel
        )
    return _deny(conn, case, result, at, base, envelope, channel)


def _deny(conn, case, result, at, base, envelope, channel) -> CaseResult:
    reason = result.explain()
    case = case_machine.transition(
        conn,
        case.case_id,
        CaseState.STOPPED,
        actor=ActorType.AGENT,
        at=at,
        rationale=f"policy denied the proposed action: {reason}"[:2000],
        stop_reason=StopReason.POLICY_BLOCKED,
        stopped_at=at,
    )
    return CaseResult(
        **base,
        outcome=Outcome.DENIED,
        action_type=envelope.action.action_type,
        channel=channel,
        effect=result.decision.effect,
        deciding_rule_id=result.decision.deciding_rule_id,
        reason=reason,
        final_state=case.state,
    )


def _await_approval(
    conn, case, diagnosis, result, at, base, envelope, channel
) -> CaseResult:
    """§9.1: ``PLANNED ──(policy: ALLOW_WITH_APPROVAL)──► AWAITING_APPROVAL``.

    The action is *not* enqueued. The outbox is exactly-once execution "awaiting
    the executor" (``_allow``); an action still waiting on a human must not be
    reachable from there, and holding a debit out of the outbox also keeps the Q1
    double-debit pre-check from firing on a proposal that may never be approved.
    The proposal survives on the ``approval_requested`` audit row; the case is
    discoverable via ``ledger.list_awaiting_approval``. The approve/reject/SLA
    edges out of AWAITING_APPROVAL are the console's job (§18.1 item 14) and are
    not built here -- so, like a quiet-hours DENY, this is where ``run`` leaves
    the case.
    """
    tier = result.decision.requires_tier
    tier_label = tier.value if tier is not None else "human"
    verb = envelope.action.action_type.value
    reason = result.explain()
    case = case_machine.transition(
        conn,
        case.case_id,
        CaseState.PLANNED,
        actor=ActorType.AGENT,
        at=at,
        rationale=f"policy allowed {verb} subject to {tier_label} approval",
        active_diagnosis_id=diagnosis.diagnosis_id,
    )
    audit_store.append(
        conn,
        ts=at,
        case_id=case.case_id,
        actor=ActorType.AGENT,
        event_type="approval_requested",
        inputs_digest=digest(
            {
                "case_id": case.case_id,
                "idempotency_key": envelope.idempotency_key,
                "action_id": envelope.action_id,
                "action_type": verb,
                "deciding_rule_id": result.decision.deciding_rule_id,
                "requires_tier": tier.value if tier else None,
            }
        ),
        policy_verdict_rule_ids=tuple(
            v.rule_id for v in result.decision.verdicts
        ),
        policy_decision=result.decision,
        policy_version=result.decision.policy_version,
        decision_rationale=(
            f"{reason} -- parked in awaiting_approval for {tier_label} sign-off; "
            "nothing enqueued"
        )[:2000],
    )
    case = case_machine.transition(
        conn,
        case.case_id,
        CaseState.AWAITING_APPROVAL,
        actor=ActorType.AGENT,
        at=at,
        rationale=f"held for {tier_label} approval: {reason}"[:2000],
    )
    return CaseResult(
        **base,
        outcome=Outcome.PENDING_APPROVAL,
        action_type=envelope.action.action_type,
        channel=channel,
        effect=result.decision.effect,
        deciding_rule_id=result.decision.deciding_rule_id,
        requires_tier=tier,
        reason=reason,
        final_state=case.state,
    )


def _allow(conn, case, diagnosis, result, at, base, envelope, channel) -> CaseResult:
    item = outbox.enqueue(conn, envelope, at=at)
    case = case_machine.transition(
        conn,
        case.case_id,
        CaseState.PLANNED,
        actor=ActorType.AGENT,
        at=at,
        rationale=f"policy allowed {envelope.action.action_type.value}",
        active_diagnosis_id=diagnosis.diagnosis_id,
    )
    audit_store.append(
        conn,
        ts=at,
        case_id=case.case_id,
        actor=ActorType.AGENT,
        event_type="action_scheduled",
        inputs_digest=digest(
            {"case_id": case.case_id, "outbox_id": item.id, "action_id": envelope.action_id}
        ),
        tool_call=envelope,
        idempotency_key=envelope.idempotency_key,
        policy_version=result.decision.policy_version,
        decision_rationale=(
            f"enqueued to the outbox as row {item.id}; "
            f"{result.explain()}"
        )[:2000],
    )
    case = case_machine.transition(
        conn,
        case.case_id,
        CaseState.SCHEDULED,
        actor=ActorType.AGENT,
        at=at,
        rationale="action enqueued and awaiting the executor",
    )
    return CaseResult(
        **base,
        outcome=Outcome.ALLOWED,
        action_type=envelope.action.action_type,
        channel=channel,
        effect=result.decision.effect,
        deciding_rule_id=result.decision.deciding_rule_id,
        reason=result.explain(),
        outbox_id=item.id,
        final_state=case.state,
    )


def run(
    conn: Connection,
    cases: Sequence[RiskCase],
    *,
    rule_set=None,
    thresholds: PolicyThresholds | None = None,
) -> list[CaseResult]:
    """Process every case, in the order given.

    ``rule_set`` defaults to one built from ``thresholds`` -- not to the module
    constant -- so a caller who changes the AFA threshold does not silently keep
    evaluating against the old literal.
    """
    resolved = thresholds if thresholds is not None else PolicyThresholds()
    resolved_rules = rule_set if rule_set is not None else build_minimal_rule_set(resolved)
    return [
        process_case(conn, case, rule_set=resolved_rules, thresholds=resolved)
        for case in cases
    ]
