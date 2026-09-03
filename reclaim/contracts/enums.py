"""Shared closed vocabularies for RECLAIM.

Everything in this module is a **frozen contract**. Adding a member is a
compatible change; renaming or removing one is a breaking change that requires a
bump of ``CONTRACTS_SCHEMA_VERSION`` in ``reclaim.contracts.versions``.

Cross-references to HACKATHON_PLAN.md are given per enum so that a reviewer can
check fidelity without re-reading the plan.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping

from pydantic import BaseModel, ConfigDict

try:  # pragma: no cover
    from enum import StrEnum
except ImportError:  # pragma: no cover
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """Minimal StrEnum shim."""


__all__ = [
    "ActorType",
    "AmountBand",
    "Arm",
    "ARM_SPECS",
    "ArmSpec",
    "AutonomyTier",
    "CaseState",
    "Channel",
    "DiagnosisSource",
    "HEADLINE_CONTROL_ARM",
    "HOLD_STOP_REASONS",
    "HEADLINE_TREATMENT_ARM",
    "HumanQueue",
    "InboundIntent",
    "Language",
    "MandateStatus",
    "MessageIntent",
    "ObligationKind",
    "ObligationStatus",
    "PlanOrigin",
    "PolicyCategory",
    "PolicyEffect",
    "PspId",
    "Rail",
    "Reversibility",
    "RiskClass",
    "RootCauseClass",
    "RuleSeverity",
    "Segment",
    "StepTrigger",
    "StopReason",
    "SuppressionScope",
    "TERMINAL_CASE_STATES",
    "ALLOWED_CASE_TRANSITIONS",
]


# ---------------------------------------------------------------------------
# Rails, channels, parties
# ---------------------------------------------------------------------------


class Rail(StrEnum):
    """Payment rails. India recurring rails carry regulatory lead times (§14.1)."""

    CARD_EMANDATE = "card_emandate"          # RBI e-mandate on card; 26h charge delay
    UPI_AUTOPAY = "upi_autopay"              # recurring cap Rs 15,000 per txn
    ENACH = "enach"                          # bank e-NACH
    CARD_ONE_TIME = "card_one_time"          # customer-present, no mandate
    UPI_COLLECT = "upi_collect"              # push/collect request, one-time
    BANK_TRANSFER = "bank_transfer"          # NEFT/RTGS/IMPS, B2B settlement


class PspId(StrEnum):
    """Payment service providers. ``SIM_PSP_2`` deliberately uses a different raw
    decline taxonomy so the normaliser cannot be Stripe-shaped (§20)."""

    STRIPE_TEST = "stripe_test"
    SIM_PSP_2 = "sim_psp_2"


class Channel(StrEnum):
    """Outbound/inbound contact channels. Not a money rail."""

    EMAIL = "email"
    WHATSAPP = "whatsapp"
    SMS = "sms"
    VOICE = "voice"
    IN_APP = "in_app"


class Language(StrEnum):
    """Content languages. ``HINGLISH`` is a distinct member because it has its own
    template set and banned-phrase list (§18.2 item 21)."""

    EN_IN = "en_IN"
    HI_IN = "hi_IN"
    HINGLISH = "hinglish"
    TA_IN = "ta_IN"
    MR_IN = "mr_IN"


class Segment(StrEnum):
    """Customer segment. Drives the financial-authority matrix and the T2 rule
    "first contact to an enterprise/strategic account" (§14.2)."""

    B2C_STANDARD = "b2c_standard"
    B2C_PREMIUM = "b2c_premium"
    B2B_SMB = "b2b_smb"
    B2B_MID_MARKET = "b2b_mid_market"
    B2B_ENTERPRISE = "b2b_enterprise"
    B2B_STRATEGIC = "b2b_strategic"

    @property
    def is_b2b(self) -> bool:
        return self.value.startswith("b2b_")

    @property
    def requires_approval_on_first_contact(self) -> bool:
        return self in (Segment.B2B_ENTERPRISE, Segment.B2B_STRATEGIC)


# ---------------------------------------------------------------------------
# Obligations
# ---------------------------------------------------------------------------


class ObligationKind(StrEnum):
    """One row per obligation in the Revenue-at-Risk Ledger (§4)."""

    SUBSCRIPTION_INVOICE = "subscription_invoice"
    B2B_INVOICE = "b2b_invoice"
    ABANDONED_CART = "abandoned_cart"


class ObligationStatus(StrEnum):
    OPEN = "open"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    CREDITED = "credited"
    WRITTEN_OFF = "written_off"
    VOID = "void"


class MandateStatus(StrEnum):
    """Mandate lifecycle. Stripe's India docs state a mandate "can't be cancelled
    or updated" via API, so recovery from a dead mandate is a *new registration*
    (§26)."""

    PENDING_REGISTRATION = "pending_registration"
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    INVALID = "invalid"
    UNKNOWN = "unknown"

    @property
    def is_debitable(self) -> bool:
        """Fail closed: only an explicitly ACTIVE mandate may be debited.

        ``UNKNOWN`` maps to False, implementing the §10.1 fallback
        "get_mandate_state: unknown -> assume invalid (fail closed: no debit)".
        """
        return self is MandateStatus.ACTIVE


# ---------------------------------------------------------------------------
# Detection & diagnosis
# ---------------------------------------------------------------------------


class RiskClass(StrEnum):
    """What a detector emits. One member per detector D1-D6 (§4, §18)."""

    FAILED_RECURRING_DEBIT = "failed_recurring_debit"            # D1
    PREDICTED_TO_FAIL_DEBIT = "predicted_to_fail_debit"          # D2 (should-build)
    OVERDUE_RECEIVABLE = "overdue_receivable"                    # D3
    CHECKOUT_ABANDONMENT = "checkout_abandonment"                # D4
    SYSTEMIC_AUTH_DEGRADATION = "systemic_auth_degradation"      # D5
    SILENT_LEAKAGE = "silent_leakage"                            # D6 (stretch)


class RootCauseClass(StrEnum):
    """The nine diagnosis hypotheses H1-H9 of §9.2, plus a fail-open member.

    NOTE the deliberate separation from ``DeclineClass``: a decline class is what
    the PSP *told us* (an observation); a root cause is what we *inferred*. The
    whole product exists because one observation
    (``PAYER_AUTHORIZATION_MISSING_AMBIGUOUS``) maps to at least two root causes
    demanding opposite actions.
    """

    H1_TIMING_LIQUIDITY = "h1_timing_liquidity"
    H2_CREDENTIAL_LIFECYCLE = "h2_credential_lifecycle"
    H3_MANDATE_DEAD_OR_PAUSED = "h3_mandate_dead_or_paused"
    H4_AFA_STEP_UP_INCOMPLETE = "h4_afa_step_up_incomplete"
    H5_DELIBERATE_CHURN_INTENT = "h5_deliberate_churn_intent"
    H6_OUR_SIDE_SYSTEMIC = "h6_our_side_systemic"
    H7_COMMERCIAL_DISPUTE = "h7_commercial_dispute"
    H8_B2B_PROCESS_DEFECT = "h8_b2b_process_defect"
    H9_B2B_LIQUIDITY_OR_WILLFUL_DELAY = "h9_b2b_liquidity_or_willful_delay"
    UNKNOWN = "unknown"

    @property
    def forbids_payment_nudge(self) -> bool:
        """Root causes where a payment nudge is the *wrong* action (§9.2)."""
        return self in (
            RootCauseClass.H5_DELIBERATE_CHURN_INTENT,
            RootCauseClass.H6_OUR_SIDE_SYSTEMIC,
            RootCauseClass.H7_COMMERCIAL_DISPUTE,
        )


class DiagnosisSource(StrEnum):
    LLM = "llm"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"
    HUMAN = "human"


class PlanOrigin(StrEnum):
    LLM_PLANNER = "llm_planner"
    DETERMINISTIC_ROUTER = "deterministic_router"
    FIXED_BASELINE_LADDER = "fixed_baseline_ladder"   # arms A1/A2
    HUMAN = "human"


# ---------------------------------------------------------------------------
# Autonomy, policy, reversibility
# ---------------------------------------------------------------------------


class AutonomyTier(StrEnum):
    """Four tiers keyed to reversibility and amount (§14.2).

    "Low confidence tiers up. Uncertainty routes to humans, never to bolder
    action." Ordering is therefore load-bearing, not cosmetic.
    """

    T0 = "T0"   # auto, silent
    T1 = "T1"   # auto, customer-visible, templated, within caps
    T2 = "T2"   # human approval required
    T3 = "T3"   # never automated

    @property
    def rank(self) -> int:
        return {"T0": 0, "T1": 1, "T2": 2, "T3": 3}[self.value]

    @property
    def requires_human_approval(self) -> bool:
        return self.rank >= AutonomyTier.T2.rank

    @property
    def is_automatable(self) -> bool:
        return self.rank <= AutonomyTier.T1.rank

    def tier_up(self, steps: int = 1) -> "AutonomyTier":
        """Escalate. Saturates at T3; never wraps around to a bolder tier."""
        order = [AutonomyTier.T0, AutonomyTier.T1, AutonomyTier.T2, AutonomyTier.T3]
        return order[min(self.rank + steps, 3)]

    @staticmethod
    def strictest(*tiers: "AutonomyTier") -> "AutonomyTier":
        """Compose tiers by taking the most restrictive. Empty -> T3 (fail closed)."""
        if not tiers:
            return AutonomyTier.T3
        return max(tiers, key=lambda t: t.rank)


class Reversibility(StrEnum):
    REVERSIBLE = "reversible"
    PARTIALLY_REVERSIBLE = "partially_reversible"
    IRREVERSIBLE = "irreversible"


class PolicyEffect(StrEnum):
    """Verdicts of the policy engine (§14.1)."""

    ALLOW = "allow"
    ALLOW_WITH_APPROVAL = "allow_with_approval"
    DEFER = "defer"
    DENY = "deny"


class PolicyCategory(StrEnum):
    """The eight rule categories of §14.1, verbatim in intent."""

    CONSENT_AND_CHANNEL = "consent_and_channel"
    TIMING = "timing"
    FREQUENCY = "frequency"
    RAIL_AND_NETWORK = "rail_and_network"
    CONTENT = "content"
    FINANCIAL_AUTHORITY = "financial_authority"
    HOLDS = "holds"
    INTEGRITY = "integrity"


class RuleSeverity(StrEnum):
    """BLOCKING rules can produce DENY/DEFER. ADVISORY rules only annotate."""

    BLOCKING = "blocking"
    ADVISORY = "advisory"


class SuppressionScope(StrEnum):
    CASE = "case"
    PAYER = "payer"
    COHORT = "cohort"


class HumanQueue(StrEnum):
    APPROVALS = "approvals"
    RETENTION = "retention"
    DISPUTES = "disputes"
    AR_ANALYST = "ar_analyst"
    INCIDENT_RESPONSE = "incident_response"
    DEAD_LETTER = "dead_letter"
    INJECTION_QUARANTINE = "injection_quarantine"


class ActorType(StrEnum):
    """Who caused an audit row (§15)."""

    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"


class MessageIntent(StrEnum):
    """Purpose of an outbound message. Bound to the template registry so that the
    banned-phrase check and DLT-template check can be selected deterministically
    (§14.1 Content)."""

    PRE_DEBIT_NOTIFICATION = "pre_debit_notification"
    PAYMENT_FAILED_INFORM = "payment_failed_inform"
    CREDENTIAL_UPDATE_REQUEST = "credential_update_request"
    MANDATE_REAUTH_REQUEST = "mandate_reauth_request"
    AFA_COMPLETION_REQUEST = "afa_completion_request"
    INVOICE_CORRECTION = "invoice_correction"
    PAYMENT_REMINDER = "payment_reminder"
    PROMISE_FOLLOW_UP = "promise_follow_up"
    RETENTION_OUTREACH = "retention_outreach"
    SERVICE_APOLOGY = "service_apology"


class InboundIntent(StrEnum):
    """Reply intents (§4 step 7). ``OPT_OUT`` recall must be 1.00 (§12.3)."""

    PROMISE = "promise"
    DISPUTE = "dispute"
    ALREADY_PAID = "already_paid"
    WRONG_RECIPIENT = "wrong_recipient"
    HARDSHIP = "hardship"
    OPT_OUT = "opt_out"
    HOSTILE = "hostile"
    QUESTION = "question"
    UNKNOWN = "unknown"

    @property
    def triggers_hard_stop(self) -> bool:
        """§9.1: dispute | opt_out -> STOPPED(hard_stop) immediately."""
        return self in (
            InboundIntent.OPT_OUT,
            InboundIntent.DISPUTE,
            InboundIntent.HARDSHIP,
        )


class StepTrigger(StrEnum):
    """Typed guard on a conditional plan step.

    CONTRACT DECISION (JC-06): the plan's "<=5-step conditional plan" (§5) uses a
    *closed* trigger vocabulary rather than free-text conditions, so the policy
    engine and scheduler can evaluate a step's precondition without an LLM.
    """

    ALWAYS = "always"
    PREV_SUCCEEDED = "prev_succeeded"
    PREV_FAILED = "prev_failed"
    NO_RESPONSE = "no_response"
    PAYMENT_NOT_RECEIVED = "payment_not_received"
    PROMISE_BREACHED = "promise_breached"
    LINK_NOT_COMPLETED = "link_not_completed"


# ---------------------------------------------------------------------------
# Case lifecycle (§9.1)
# ---------------------------------------------------------------------------


class CaseState(StrEnum):
    DETECTED = "detected"
    SUPPRESSED = "suppressed"
    DIAGNOSING = "diagnosing"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    AWAITING_RESPONSE = "awaiting_response"
    PROMISED = "promised"
    RECONCILING = "reconciling"
    RETRY_BACKOFF = "retry_backoff"
    PARTIALLY_RECOVERED = "partially_recovered"
    ESCALATED = "escalated"
    RECOVERED = "recovered"
    STOPPED = "stopped"
    WRITTEN_OFF = "written_off"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_CASE_STATES


TERMINAL_CASE_STATES: frozenset[CaseState] = frozenset(
    {CaseState.RECOVERED, CaseState.STOPPED, CaseState.WRITTEN_OFF}
)


class StopReason(StrEnum):
    """Reason attached to ``STOPPED`` (§9.1, §14.3)."""

    POLICY_BLOCKED = "policy_blocked"
    HUMAN_REJECTED = "human_rejected"
    APPROVAL_TIMEOUT = "approval_timeout"
    HARD_STOP_OPT_OUT = "hard_stop_opt_out"
    HARD_STOP_DISPUTE = "hard_stop_dispute"
    HARD_STOP_HARDSHIP = "hard_stop_hardship"
    HARD_STOP_LEGAL_HOLD = "hard_stop_legal_hold"
    HARD_STOP_BEREAVEMENT = "hard_stop_bereavement"
    HARD_STOP_CHARGEBACK = "hard_stop_chargeback"
    CONTACT_CAP = "contact_cap"
    HARD_DECLINE_NO_FURTHER_DEBIT = "hard_decline_no_further_debit"
    ALREADY_PAID = "already_paid"
    SYSTEMIC_INCIDENT_OURS = "systemic_incident_ours"
    CONFIDENCE_FLOOR_TWICE = "confidence_floor_twice"
    WRONG_RECIPIENT = "wrong_recipient"
    OBLIGATION_VOIDED = "obligation_voided"


#: The seven members of §14.1's **Holds** row -- "immediate hard stop": opt-out,
#: active dispute, hardship/vulnerability, bereavement, legal hold, chargeback in
#: progress, and an open systemic incident attributable to us.
#:
#: A hold and a stop share one vocabulary on purpose. ``Hold.kind`` was a free
#: string listing these seven in prose, so ``'optout'`` constructed cleanly and
#: matched nothing downstream (CONTRACTS.md §7 N5). The remaining ``StopReason``
#: members stop a *ladder* -- a contact cap, an approval timeout, an already-paid
#: reconciliation -- and are deliberately excluded: a hold carrying one would
#: suppress contact that policy still permits.
HOLD_STOP_REASONS: frozenset[StopReason] = frozenset(
    {
        StopReason.HARD_STOP_OPT_OUT,
        StopReason.HARD_STOP_DISPUTE,
        StopReason.HARD_STOP_HARDSHIP,
        StopReason.HARD_STOP_BEREAVEMENT,
        StopReason.HARD_STOP_LEGAL_HOLD,
        StopReason.HARD_STOP_CHARGEBACK,
        StopReason.SYSTEMIC_INCIDENT_OURS,
    }
)


#: The state machine of §9.1, frozen. Phase 1 must reject any transition absent
#: from this table (runtime invariant #9 depends on it). ``STOPPED`` /
#: ``RECOVERED`` / ``WRITTEN_OFF`` have no outgoing edges by construction.
ALLOWED_CASE_TRANSITIONS: Mapping[CaseState, frozenset[CaseState]] = {
    CaseState.DETECTED: frozenset(
        {CaseState.PLANNED, CaseState.SUPPRESSED, CaseState.DIAGNOSING, CaseState.STOPPED}
    ),
    CaseState.SUPPRESSED: frozenset({CaseState.DETECTED, CaseState.STOPPED}),
    CaseState.DIAGNOSING: frozenset({CaseState.PLANNED, CaseState.ESCALATED, CaseState.STOPPED}),
    CaseState.PLANNED: frozenset(
        {CaseState.SCHEDULED, CaseState.AWAITING_APPROVAL, CaseState.STOPPED}
    ),
    CaseState.AWAITING_APPROVAL: frozenset({CaseState.SCHEDULED, CaseState.STOPPED}),
    CaseState.SCHEDULED: frozenset({CaseState.EXECUTING, CaseState.STOPPED, CaseState.PLANNED}),
    CaseState.EXECUTING: frozenset(
        {
            CaseState.RECOVERED,
            CaseState.PARTIALLY_RECOVERED,
            CaseState.AWAITING_RESPONSE,
            CaseState.RETRY_BACKOFF,
            CaseState.STOPPED,
        }
    ),
    CaseState.RETRY_BACKOFF: frozenset({CaseState.EXECUTING, CaseState.ESCALATED, CaseState.STOPPED}),
    CaseState.PARTIALLY_RECOVERED: frozenset({CaseState.PLANNED, CaseState.RECOVERED, CaseState.STOPPED}),
    CaseState.AWAITING_RESPONSE: frozenset(
        {
            CaseState.PROMISED,
            CaseState.STOPPED,
            CaseState.RECONCILING,
            CaseState.PLANNED,
            CaseState.ESCALATED,
        }
    ),
    CaseState.PROMISED: frozenset({CaseState.RECOVERED, CaseState.PLANNED, CaseState.STOPPED}),
    CaseState.RECONCILING: frozenset({CaseState.RECOVERED, CaseState.PLANNED, CaseState.STOPPED}),
    CaseState.ESCALATED: frozenset(
        {CaseState.RECOVERED, CaseState.STOPPED, CaseState.WRITTEN_OFF, CaseState.PLANNED}
    ),
    CaseState.RECOVERED: frozenset(),
    CaseState.STOPPED: frozenset(),
    CaseState.WRITTEN_OFF: frozenset(),
}


def is_transition_allowed(src: CaseState, dst: CaseState) -> bool:
    return dst in ALLOWED_CASE_TRANSITIONS.get(src, frozenset())


# ---------------------------------------------------------------------------
# Experiment arms (§12.2)
# ---------------------------------------------------------------------------


class Arm(StrEnum):
    """The ablation ladder. Membership is immutable once assigned (§12.1)."""

    A0 = "A0"
    A1 = "A1"
    A2 = "A2"
    A3 = "A3"
    A4 = "A4"
    A5 = "A5"


class ArmSpec(BaseModel):
    """What each arm is allowed to switch on. Frozen configuration, not runtime state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: Arm
    label: str
    isolates: str
    takes_any_action: bool
    baseline_ladder_enabled: bool
    hazard_timing_enabled: bool
    deterministic_routing_enabled: bool
    llm_enabled: bool
    policy_engine_enabled: bool
    simulation_only: bool


#: CONTRACT DECISION (JC-08): ``A5`` disables the policy engine, so it must never
#: touch a real rail or a real channel. ``simulation_only=True`` is enforced by a
#: contract test and must be re-checked by the execution engine in Phase 1.
ARM_SPECS: Mapping[Arm, ArmSpec] = {
    Arm.A0: ArmSpec(
        arm=Arm.A0,
        label="No action",
        isolates="Natural recovery. The number a naive submission reports as its own.",
        takes_any_action=False,
        baseline_ladder_enabled=False,
        hazard_timing_enabled=False,
        deterministic_routing_enabled=False,
        llm_enabled=False,
        policy_engine_enabled=True,
        simulation_only=False,
    ),
    Arm.A1: ArmSpec(
        arm=Arm.A1,
        label="Fixed schedule + static 4-email drip",
        isolates="The realistic industry baseline.",
        takes_any_action=True,
        baseline_ladder_enabled=True,
        hazard_timing_enabled=False,
        deterministic_routing_enabled=False,
        llm_enabled=False,
        policy_engine_enabled=True,
        simulation_only=False,
    ),
    Arm.A2: ArmSpec(
        arm=Arm.A2,
        label="A1 + hazard timing model",
        isolates="Value of ML timing.",
        takes_any_action=True,
        baseline_ladder_enabled=True,
        hazard_timing_enabled=True,
        deterministic_routing_enabled=False,
        llm_enabled=False,
        policy_engine_enabled=True,
        simulation_only=False,
    ),
    Arm.A3: ArmSpec(
        arm=Arm.A3,
        label="A2 + deterministic diagnosis->intervention routing",
        isolates="Value of intervention choice, without an LLM.",
        takes_any_action=True,
        baseline_ladder_enabled=True,
        hazard_timing_enabled=True,
        deterministic_routing_enabled=True,
        llm_enabled=False,
        policy_engine_enabled=True,
        simulation_only=False,
    ),
    Arm.A4: ArmSpec(
        arm=Arm.A4,
        label="A3 + LLM diagnosis, planner, personalisation, reply understanding",
        isolates="Value of the LLM -- measured, not asserted.",
        takes_any_action=True,
        baseline_ladder_enabled=True,
        hazard_timing_enabled=True,
        deterministic_routing_enabled=True,
        llm_enabled=True,
        policy_engine_enabled=True,
        simulation_only=False,
    ),
    Arm.A5: ArmSpec(
        arm=Arm.A5,
        label="A4 with the policy engine disabled",
        isolates="The price of compliance: recovery gained vs violations incurred.",
        takes_any_action=True,
        baseline_ladder_enabled=True,
        hazard_timing_enabled=True,
        deterministic_routing_enabled=True,
        llm_enabled=True,
        policy_engine_enabled=False,
        simulation_only=True,
    ),
}

#: §12.1: "Arms: control (A1) + treatment (A4)". A0 is the natural-recovery
#: reference, reported alongside but not the control for the headline estimate.
HEADLINE_CONTROL_ARM: Arm = Arm.A1
HEADLINE_TREATMENT_ARM: Arm = Arm.A4


# ---------------------------------------------------------------------------
# Stratification bands
# ---------------------------------------------------------------------------


class AmountBand(StrEnum):
    """Frozen stratification bands.

    CONTRACT DECISION (JC-02): the boundaries mirror the *policy* thresholds
    (Rs 2,000 T0 ceiling, Rs 15,000 AFA threshold, Rs 1,00,000 category
    relaxation) but are **deliberately decoupled from config**. If the AFA
    threshold moves in ``policy/thresholds.yaml`` mid-run, strata must not move,
    or the experiment's stratum-weighted estimator becomes invalid.
    """

    LE_2K = "le_2k"
    LE_15K = "le_15k"
    LE_1L = "le_1l"
    LE_10L = "le_10l"
    GT_10L = "gt_10l"
