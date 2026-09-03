"""The typed action catalog -- the only verbs that exist.

This is the narrowest point in RECLAIM, and deliberately so. The LLM cannot call
anything; it can only *propose* a member of the ``Action`` union. The policy engine
vetoes members of that union. The executor executes members of that union. A verb
that is not in ``ActionType`` cannot be named, proposed, approved, executed or
logged -- which is why §14.4's answer to "what if the model is fully compromised?"
is structural rather than aspirational.

Three properties are load-bearing and each is pinned by a test:

1. **Closure.** ``ActionType`` has exactly the thirteen write verbs of §10.2, and
   ``FORBIDDEN_VERBS`` names the nine that must never appear. The intersection is
   asserted empty at import time.
2. **No prose channel.** Every parameter is typed and bounded. ``extra="forbid"``
   means a hallucinated ``discount_pct`` is a validation error, not a silently
   dropped field. ``send_message`` has no ``body``: the model fills named slots in
   a registered template (JC-17 below).
3. **Derived idempotency.** ``ActionEnvelope.idempotency_key`` is computed from the
   canonical digest of the action's semantic scope. A caller cannot supply one, so
   invariant #8 ("exactly one idempotency key per external action") cannot be
   subverted by the component with the most incentive to retry.

CONTRACT DECISION (JC-17) -- how ``send_message`` reconciles §7 with §14.1
--------------------------------------------------------------------------
§7 gives the LLM ownership of drafting, tone and Hinglish register. §14.1 requires
DLT-registered templates on SMS and a banned-phrase check on everything. Free-form
generation cannot satisfy both. The resolution: the LLM chooses a **registered
template**, a **language**, a **tone** from a closed vocabulary, and the **values
of named slots**. Slot values are length-capped and content-checked. A slot may be
marked ``free_text`` only where the template permits it and never on SMS, where
registration is per-template. The model therefore controls *what the message says*
without controlling *what the message is*.

CONTRACT DECISION (JC-18) -- tiers here are floors, not verdicts
----------------------------------------------------------------
``ActionSpec.base_tier`` is the catalog's floor for an action type. §14.2's full
tier calculation also reads amount, diagnosis confidence, customer tier and
failure-class novelty, and it may only ever raise the tier ("low confidence tiers
up"). That calculator is policy logic and belongs to the policy engine, not to
this module; the catalog fixes the floor it may not go below.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Final, Literal, Mapping, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from reclaim.contracts.canonical import digest
from reclaim.contracts.enums import (
    AutonomyTier,
    Channel,
    HumanQueue,
    Language,
    MessageIntent,
    PlanOrigin,
    PolicyCategory,
    Rail,
    Reversibility,
    RootCauseClass,
    SuppressionScope,
)
from reclaim.contracts.ids import (
    ActionId,
    CaseId,
    CohortId,
    HoldId,
    IncidentId,
    MandateId,
    NotificationId,
    ObligationId,
    PayerId,
)
from reclaim.contracts.money import Money
from reclaim.contracts.rails import rail_spec
from reclaim.contracts.temporal import UtcDatetime
from reclaim.contracts.versions import ACTION_CATALOG_VERSION

__all__ = [
    "ACTION_CATALOG_VERSION",
    "ACTION_MODELS",
    "ACTION_SPECS",
    "FORBIDDEN_VERBS",
    "MAX_GRACE_PERIOD_DAYS",
    "MAX_INSTALMENTS",
    "MAX_SLOTS_PER_MESSAGE",
    "MAX_SLOT_VALUE_CHARS",
    "Action",
    "ActionEnvelope",
    "ActionSpec",
    "ActionType",
    "ApplyGracePeriod",
    "CreateCredentialUpdateLink",
    "CreateMandateReauthLink",
    "EscalateToHuman",
    "InitiateVoiceCall",
    "MessageTone",
    "OfferPaymentPlan",
    "OpenSystemicIncident",
    "PaymentPlanInstalment",
    "ProposeRouteChange",
    "RecommendWriteOff",
    "ScheduleDebit",
    "SendMessage",
    "SendPreDebitNotification",
    "SuppressContact",
    "TemplateSlotValue",
    "action_spec",
    "idempotency_key",
    "tool_schemas_for_llm",
]


class ActionType(str, Enum):
    """The thirteen write verbs of HACKATHON_PLAN.md §10.2. There are no others."""

    SCHEDULE_DEBIT = "schedule_debit"
    SEND_PRE_DEBIT_NOTIFICATION = "send_pre_debit_notification"
    SEND_MESSAGE = "send_message"
    CREATE_MANDATE_REAUTH_LINK = "create_mandate_reauth_link"
    CREATE_CREDENTIAL_UPDATE_LINK = "create_credential_update_link"
    OFFER_PAYMENT_PLAN = "offer_payment_plan"
    APPLY_GRACE_PERIOD = "apply_grace_period"
    INITIATE_VOICE_CALL = "initiate_voice_call"
    SUPPRESS_CONTACT = "suppress_contact"
    OPEN_SYSTEMIC_INCIDENT = "open_systemic_incident"
    PROPOSE_ROUTE_CHANGE = "propose_route_change"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    RECOMMEND_WRITE_OFF = "recommend_write_off"


#: §10.2's deliberately absent verbs. Listed explicitly so that their absence is a
#: tested property rather than something a future contributor might "helpfully" add.
FORBIDDEN_VERBS: Final[frozenset[str]] = frozenset(
    {
        "mark_invoice_paid",
        "apply_discount",
        "waive_fee",
        "cancel_subscription",
        "suspend_service",
        "report_to_bureau",
        "contact_third_party",
        "modify_policy",
        "delete_audit_row",
    }
)

MAX_SLOT_VALUE_CHARS: Final[int] = 240
MAX_SLOTS_PER_MESSAGE: Final[int] = 12
MAX_INSTALMENTS: Final[int] = 12
MAX_GRACE_PERIOD_DAYS: Final[int] = 90

_TemplateId = Annotated[str, StringConstraints(pattern=r"^tpl_[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
_ScriptId = Annotated[str, StringConstraints(pattern=r"^scr_[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
_SlotName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
_NonEmpty = Annotated[str, StringConstraints(min_length=1, max_length=2000, strip_whitespace=True)]

#: Channels that can carry an auditable pre-debit notification. Voice is excluded:
#: it leaves the customer no record of the exact amount and no opt-out affordance.
_NOTIFIABLE_CHANNELS: Final[frozenset[Channel]] = frozenset(
    {Channel.EMAIL, Channel.WHATSAPP, Channel.SMS, Channel.IN_APP}
)


class MessageTone(str, Enum):
    """Closed tone vocabulary. The LLM picks one; it does not invent register."""

    NEUTRAL = "neutral"
    WARM = "warm"
    FIRM = "firm"
    APOLOGETIC = "apologetic"


class _ActionBase(BaseModel):
    """Common configuration for every action.

    ``extra="forbid"`` is the important line in this file: it converts a
    hallucinated parameter into a validation error at the boundary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)

    def idempotency_scope(self, case_id: str) -> dict[str, Any]:
        """The fields that make two proposals *the same act*.

        Default: the case plus every parameter. Subclasses narrow this only where
        the semantics demand it, and each override is pinned by a test.
        """
        return {"case_id": case_id, "params": self.model_dump(mode="json")}


# --------------------------------------------------------------- money movement


class ScheduleDebit(_ActionBase):
    """Submit a debit. The only verb in the catalog that moves money."""

    action_type: Literal[ActionType.SCHEDULE_DEBIT] = ActionType.SCHEDULE_DEBIT
    obligation_id: ObligationId
    rail: Rail
    amount: Money
    execute_at: UtcDatetime
    attempt_sequence: int = Field(ge=1, le=64, description="1-based; counts toward network retry limits.")
    mandate_id: MandateId | None = None
    pre_debit_notification_id: NotificationId | None = Field(
        default=None,
        description="Required on any rail that mandates a pre-debit notification. "
        "Invariant #4 therefore has a structural half: the debit cannot be "
        "expressed without pointing at the notification that preceded it.",
    )

    @model_validator(mode="after")
    def _rail_shape(self) -> "ScheduleDebit":
        spec = rail_spec(self.rail)
        if not spec.permits_debit_request:
            raise ValueError(
                f"{self.rail.value} is push-only: a debit cannot be submitted on it"
            )
        if self.amount.paise <= 0:
            raise ValueError("a debit must be for a positive amount")
        if spec.is_mandate_backed and self.mandate_id is None:
            raise ValueError(f"{self.rail.value} debits require a mandate reference")
        if spec.requires_pre_debit_notification and self.pre_debit_notification_id is None:
            raise ValueError(
                f"{self.rail.value} requires a pre-debit notification at least "
                f"{spec.pre_debit_notification_min_lead_hours}h before the debit; "
                "reference the notification that was sent"
            )
        if spec.max_per_transaction is not None and self.amount > spec.max_per_transaction:
            raise ValueError(
                f"{self.amount} exceeds the {self.rail.value} per-transaction "
                f"ceiling of {spec.max_per_transaction}"
            )
        return self


class SendPreDebitNotification(_ActionBase):
    """The mandatory pre-debit notice. Carries the **exact** amount (§11.2)."""

    action_type: Literal[ActionType.SEND_PRE_DEBIT_NOTIFICATION] = (
        ActionType.SEND_PRE_DEBIT_NOTIFICATION
    )
    obligation_id: ObligationId
    mandate_id: MandateId
    rail: Rail
    channel: Channel
    language: Language
    amount: Money
    debit_scheduled_at: UtcDatetime
    includes_opt_out_instruction: Literal[True] = True

    @model_validator(mode="after")
    def _notifiable(self) -> "SendPreDebitNotification":
        if not rail_spec(self.rail).requires_pre_debit_notification:
            raise ValueError(
                f"{self.rail.value} has no pre-debit notification requirement; "
                "sending one would misrepresent the rail to the customer"
            )
        if self.channel not in _NOTIFIABLE_CHANNELS:
            raise ValueError(
                f"a pre-debit notification cannot be delivered on {self.channel.value}: "
                "the customer must retain a record of the exact amount and an opt-out"
            )
        if self.amount.paise <= 0:
            raise ValueError("a pre-debit notification must state a positive amount")
        return self


# ----------------------------------------------------------------- contact


class TemplateSlotValue(BaseModel):
    """One named slot in a registered template."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: _SlotName
    value: Annotated[str, StringConstraints(max_length=MAX_SLOT_VALUE_CHARS)]
    free_text: bool = Field(
        default=False,
        description="True only for a slot the template declares as free prose. "
        "Never permitted on SMS, where DLT registration is per-template.",
    )


class SendMessage(_ActionBase):
    """Contact the customer through a registered template (JC-17)."""

    action_type: Literal[ActionType.SEND_MESSAGE] = ActionType.SEND_MESSAGE
    channel: Channel
    template_id: _TemplateId
    language: Language
    intent: MessageIntent
    slots: tuple[TemplateSlotValue, ...] = Field(default=(), max_length=MAX_SLOTS_PER_MESSAGE)
    tone: MessageTone = MessageTone.NEUTRAL

    @field_validator("slots")
    @classmethod
    def _unique_and_ordered(cls, slots: tuple[TemplateSlotValue, ...]) -> tuple[TemplateSlotValue, ...]:
        names = [s.name for s in slots]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate template slots: {sorted(duplicates)}")
        # Sorted so that slot ordering cannot change the idempotency key, and a
        # model that lists slots in a different order does not double-send.
        return tuple(sorted(slots, key=lambda s: s.name))

    @model_validator(mode="after")
    def _sms_is_template_only(self) -> "SendMessage":
        if self.channel is Channel.VOICE:
            raise ValueError("voice contact is initiate_voice_call, not send_message")
        if self.channel is Channel.SMS and self.has_free_text_slot:
            raise ValueError(
                "SMS templates are DLT-registered per template: a free-text slot "
                "would send unregistered content"
            )
        return self

    @property
    def has_free_text_slot(self) -> bool:
        return any(s.free_text for s in self.slots)


class InitiateVoiceCall(_ActionBase):
    """An automated call. T2 always: disclosure and recording notice are structural."""

    action_type: Literal[ActionType.INITIATE_VOICE_CALL] = ActionType.INITIATE_VOICE_CALL
    payer_id: PayerId
    #: Fixed, but present. §14.1's consent, quiet-hours and frequency gates read
    #: the channel off every contact action through ``ActionSpec.channel_field``;
    #: a voice call that omitted it would be the one contact action where all
    #: three gates raise AttributeError instead of running. ``Literal`` keeps it
    #: from being anything else -- the channel of a voice call is not a choice.
    channel: Literal[Channel.VOICE] = Channel.VOICE
    script_id: _ScriptId
    language: Language
    discloses_automated_call: Literal[True] = True
    gives_recording_notice: Literal[True] = True


class CreateMandateReauthLink(_ActionBase):
    """Start a mandate re-registration journey. Reversible via link expiry."""

    action_type: Literal[ActionType.CREATE_MANDATE_REAUTH_LINK] = (
        ActionType.CREATE_MANDATE_REAUTH_LINK
    )
    payer_id: PayerId
    rail: Rail
    expires_at: UtcDatetime
    replaces_mandate_id: MandateId | None = None

    @model_validator(mode="after")
    def _rail_is_mandate_backed(self) -> "CreateMandateReauthLink":
        if not rail_spec(self.rail).is_mandate_backed:
            raise ValueError(f"{self.rail.value} has no mandate to re-authorise")
        return self


class CreateCredentialUpdateLink(_ActionBase):
    """Start a card/credential update journey. Reversible via link expiry."""

    action_type: Literal[ActionType.CREATE_CREDENTIAL_UPDATE_LINK] = (
        ActionType.CREATE_CREDENTIAL_UPDATE_LINK
    )
    payer_id: PayerId
    expires_at: UtcDatetime


# ------------------------------------------------------- commercial (T2 always)


class PaymentPlanInstalment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    due_at: UtcDatetime
    amount: Money


class OfferPaymentPlan(_ActionBase):
    """Reschedule the *same* amount. No discount exists anywhere in the catalog."""

    action_type: Literal[ActionType.OFFER_PAYMENT_PLAN] = ActionType.OFFER_PAYMENT_PLAN
    obligation_id: ObligationId
    total_amount: Money
    instalments: tuple[PaymentPlanInstalment, ...] = Field(min_length=2, max_length=MAX_INSTALMENTS)

    @model_validator(mode="after")
    def _sums_exactly_and_is_ordered(self) -> "OfferPaymentPlan":
        total = sum(i.amount.paise for i in self.instalments)
        if total != self.total_amount.paise:
            raise ValueError(
                f"instalments sum to {total} paise but the obligation is "
                f"{self.total_amount.paise} paise. A plan that does not sum exactly "
                "is a concession, and agent concession authority is Rs 0 (invariant #7)"
            )
        due = [i.due_at for i in self.instalments]
        if due != sorted(due) or len(set(due)) != len(due):
            raise ValueError("instalment due dates must be strictly increasing")
        return self

    @property
    def instalment_count(self) -> int:
        return len(self.instalments)


class ApplyGracePeriod(_ActionBase):
    """Delay suspension. Reversible; capped by segment in policy configuration."""

    action_type: Literal[ActionType.APPLY_GRACE_PERIOD] = ActionType.APPLY_GRACE_PERIOD
    obligation_id: ObligationId
    days: int = Field(ge=1, le=MAX_GRACE_PERIOD_DAYS)
    rationale: _NonEmpty


# -------------------------------------------------------------- internal verbs


class SuppressContact(_ActionBase):
    """Prevent outreach. The safe direction, but it still needs a provenance."""

    action_type: Literal[ActionType.SUPPRESS_CONTACT] = ActionType.SUPPRESS_CONTACT
    scope: SuppressionScope
    scope_ref: _NonEmpty
    reason: _NonEmpty
    until: UtcDatetime
    incident_id: IncidentId | None = None
    hold_id: HoldId | None = None

    @model_validator(mode="after")
    def _reason_has_a_referent(self) -> "SuppressContact":
        if self.incident_id is None and self.hold_id is None:
            raise ValueError(
                "§10.2 guard: a suppression reason must reference an incident or a "
                "hold, so that the ledger can explain why contact stopped"
            )
        return self


class OpenSystemicIncident(_ActionBase):
    """Raise an internal incident for a cohort. Deduped by cohort downstream."""

    action_type: Literal[ActionType.OPEN_SYSTEMIC_INCIDENT] = ActionType.OPEN_SYSTEMIC_INCIDENT
    cohort_id: CohortId
    hypothesis: _NonEmpty
    suspected_root_cause: RootCauseClass | None = None


class ProposeRouteChange(_ActionBase):
    """Propose -- never apply -- a routing/config change. T2, blast-radius bounded.

    ``change_description`` carries the proposed diff in reviewable form; the
    applied diff is produced by the configuration system, not by the agent.
    """

    action_type: Literal[ActionType.PROPOSE_ROUTE_CHANGE] = ActionType.PROPOSE_ROUTE_CHANGE
    cohort_id: CohortId
    change_description: _NonEmpty
    rollback_plan: _NonEmpty


class EscalateToHuman(_ActionBase):
    """The safe default. Always permitted, at every tier, in every state."""

    action_type: Literal[ActionType.ESCALATE_TO_HUMAN] = ActionType.ESCALATE_TO_HUMAN
    queue: HumanQueue
    reason: _NonEmpty
    evidence_pack_ref: str | None = None


class RecommendWriteOff(_ActionBase):
    """Advice only (T3). Carries no amount, because the agent cannot write anything
    off -- a human decides and a different system executes."""

    action_type: Literal[ActionType.RECOMMEND_WRITE_OFF] = ActionType.RECOMMEND_WRITE_OFF
    obligation_id: ObligationId
    rationale: _NonEmpty


# ------------------------------------------------------------------- the union

Action = Annotated[
    Union[
        ScheduleDebit,
        SendPreDebitNotification,
        SendMessage,
        CreateMandateReauthLink,
        CreateCredentialUpdateLink,
        OfferPaymentPlan,
        ApplyGracePeriod,
        InitiateVoiceCall,
        SuppressContact,
        OpenSystemicIncident,
        ProposeRouteChange,
        EscalateToHuman,
        RecommendWriteOff,
    ],
    Field(discriminator="action_type"),
]

ACTION_MODELS: Mapping[ActionType, type[_ActionBase]] = {
    ActionType.SCHEDULE_DEBIT: ScheduleDebit,
    ActionType.SEND_PRE_DEBIT_NOTIFICATION: SendPreDebitNotification,
    ActionType.SEND_MESSAGE: SendMessage,
    ActionType.CREATE_MANDATE_REAUTH_LINK: CreateMandateReauthLink,
    ActionType.CREATE_CREDENTIAL_UPDATE_LINK: CreateCredentialUpdateLink,
    ActionType.OFFER_PAYMENT_PLAN: OfferPaymentPlan,
    ActionType.APPLY_GRACE_PERIOD: ApplyGracePeriod,
    ActionType.INITIATE_VOICE_CALL: InitiateVoiceCall,
    ActionType.SUPPRESS_CONTACT: SuppressContact,
    ActionType.OPEN_SYSTEMIC_INCIDENT: OpenSystemicIncident,
    ActionType.PROPOSE_ROUTE_CHANGE: ProposeRouteChange,
    ActionType.ESCALATE_TO_HUMAN: EscalateToHuman,
    ActionType.RECOMMEND_WRITE_OFF: RecommendWriteOff,
}


# ------------------------------------------------------------------ the specs


class ActionSpec(BaseModel):
    """Catalog metadata. The policy engine and the planner both read this.

    Nothing here is behaviour; it is the declarative description of an action that
    lets a *generic* policy engine decide which rule categories to evaluate,
    without a per-verb branch in the engine.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_type: ActionType
    side_effect: str
    reversibility: Reversibility
    base_tier: AutonomyTier = Field(description="Floor, not verdict -- see JC-18.")
    never_below_base_tier: bool = Field(
        description="True where §10.2 says 'always' (payment plan, voice call, "
        "route change): earned autonomy may never promote this action type."
    )
    moves_money: bool
    customer_visible: bool = Field(description="The customer perceives the effect.")
    is_outbound_contact: bool = Field(
        description="We put a message in front of the customer. Distinct from "
        "customer_visible: a debit is visible but is authorised by a mandate, not "
        "by contact consent, and must not consume the contact frequency cap."
    )
    channel_field: str | None = Field(
        default=None, description="Name of the field carrying the Channel, if any."
    )
    requires_consent: bool
    requires_quiet_hours_check: bool
    counts_toward_frequency_cap: bool
    requires_valid_mandate: bool
    requires_reconciliation_check: bool = Field(
        description="§14.3: the already-paid check runs before every contact and "
        "before any money movement."
    )
    is_recommendation_only: bool = Field(
        default=False, description="The agent can never execute it (T3 advice)."
    )
    is_config_change: bool = Field(default=False, description="Blast radius beyond one case.")
    always_permitted: bool = Field(
        default=False,
        description="Escalation only. The safe default must never be blocked, or a "
        "stuck case has nowhere to go.",
    )
    policy_categories: frozenset[PolicyCategory] = Field(
        description="Rule categories that MUST be evaluated before this action."
    )
    guard_summary: str


_CONSENT = PolicyCategory.CONSENT_AND_CHANNEL
_TIMING = PolicyCategory.TIMING
_FREQ = PolicyCategory.FREQUENCY
_RAIL = PolicyCategory.RAIL_AND_NETWORK
_CONTENT = PolicyCategory.CONTENT
_FINANCE = PolicyCategory.FINANCIAL_AUTHORITY
_HOLDS = PolicyCategory.HOLDS
_INTEGRITY = PolicyCategory.INTEGRITY

_CONTACT_CATEGORIES = frozenset({_CONSENT, _TIMING, _FREQ, _CONTENT, _HOLDS, _INTEGRITY})

ACTION_SPECS: Mapping[ActionType, ActionSpec] = {
    ActionType.SCHEDULE_DEBIT: ActionSpec(
        action_type=ActionType.SCHEDULE_DEBIT,
        side_effect="money movement",
        reversibility=Reversibility.IRREVERSIBLE,
        base_tier=AutonomyTier.T0,
        never_below_base_tier=False,
        moves_money=True,
        customer_visible=True,
        is_outbound_contact=False,
        requires_consent=False,
        requires_quiet_hours_check=False,
        counts_toward_frequency_cap=False,
        requires_valid_mandate=True,
        requires_reconciliation_check=True,
        # _FINANCE is here because the guard_summary's "T2 above the AFA
        # threshold" is a financial-authority gate and policy_categories is what
        # a generic engine iterates: without it the one money-moving verb in the
        # catalog asked for no financial-authority rule at all (CONTRACTS.md §7
        # N6). _TIMING is deliberately absent -- §14.1 files the >=24h pre-debit
        # notification under Rail / network, already present, and every Timing
        # rule (quiet hours, declared holidays, contact windows) is scoped to
        # contact, which a mandate-authorised debit is not.
        policy_categories=frozenset({_RAIL, _FINANCE, _HOLDS, _INTEGRITY}),
        guard_summary="mandate valid; amount <= cap; pre-debit notification >=24h; "
        "network retry count; idempotency key. T2 above the AFA threshold.",
    ),
    ActionType.SEND_PRE_DEBIT_NOTIFICATION: ActionSpec(
        action_type=ActionType.SEND_PRE_DEBIT_NOTIFICATION,
        side_effect="customer notified",
        reversibility=Reversibility.IRREVERSIBLE,
        base_tier=AutonomyTier.T0,
        never_below_base_tier=False,
        moves_money=False,
        customer_visible=True,
        is_outbound_contact=True,
        channel_field="channel",
        requires_consent=True,
        requires_quiet_hours_check=True,
        counts_toward_frequency_cap=True,
        requires_valid_mandate=True,
        requires_reconciliation_check=True,
        policy_categories=_CONTACT_CATEGORIES | {_RAIL},
        guard_summary="mandatory before any India debit; exact amount required.",
    ),
    ActionType.SEND_MESSAGE: ActionSpec(
        action_type=ActionType.SEND_MESSAGE,
        side_effect="customer contacted",
        reversibility=Reversibility.IRREVERSIBLE,
        base_tier=AutonomyTier.T1,
        never_below_base_tier=False,
        moves_money=False,
        customer_visible=True,
        is_outbound_contact=True,
        channel_field="channel",
        requires_consent=True,
        requires_quiet_hours_check=True,
        counts_toward_frequency_cap=True,
        requires_valid_mandate=False,
        requires_reconciliation_check=True,
        policy_categories=_CONTACT_CATEGORIES,
        guard_summary="consent, quiet hours, frequency cap, DLT template, "
        "banned-phrase check. T2 on first contact to an enterprise account.",
    ),
    ActionType.CREATE_MANDATE_REAUTH_LINK: ActionSpec(
        action_type=ActionType.CREATE_MANDATE_REAUTH_LINK,
        side_effect="customer journey started",
        reversibility=Reversibility.REVERSIBLE,
        base_tier=AutonomyTier.T1,
        never_below_base_tier=False,
        moves_money=False,
        customer_visible=True,
        is_outbound_contact=False,
        requires_consent=False,
        requires_quiet_hours_check=False,
        counts_toward_frequency_cap=False,
        requires_valid_mandate=False,
        requires_reconciliation_check=True,
        policy_categories=frozenset({_HOLDS, _INTEGRITY, _RAIL}),
        guard_summary="one active link per payer; expiry set. Creating the link is "
        "not contact; delivering it is a separate send_message.",
    ),
    ActionType.CREATE_CREDENTIAL_UPDATE_LINK: ActionSpec(
        action_type=ActionType.CREATE_CREDENTIAL_UPDATE_LINK,
        side_effect="customer journey started",
        reversibility=Reversibility.REVERSIBLE,
        base_tier=AutonomyTier.T1,
        never_below_base_tier=False,
        moves_money=False,
        customer_visible=True,
        is_outbound_contact=False,
        requires_consent=False,
        requires_quiet_hours_check=False,
        counts_toward_frequency_cap=False,
        requires_valid_mandate=False,
        requires_reconciliation_check=True,
        policy_categories=frozenset({_HOLDS, _INTEGRITY}),
        guard_summary="one active link per payer; expiry set.",
    ),
    ActionType.OFFER_PAYMENT_PLAN: ActionSpec(
        action_type=ActionType.OFFER_PAYMENT_PLAN,
        side_effect="commercial commitment",
        reversibility=Reversibility.PARTIALLY_REVERSIBLE,
        base_tier=AutonomyTier.T2,
        never_below_base_tier=True,
        moves_money=False,
        customer_visible=True,
        is_outbound_contact=False,
        requires_consent=False,
        requires_quiet_hours_check=False,
        counts_toward_frequency_cap=False,
        requires_valid_mandate=False,
        requires_reconciliation_check=True,
        policy_categories=frozenset({_FINANCE, _HOLDS, _INTEGRITY}),
        guard_summary="schedule within the segment authority matrix; human approves "
        "always; instalments must sum to the full amount (no discount).",
    ),
    ActionType.APPLY_GRACE_PERIOD: ActionSpec(
        action_type=ActionType.APPLY_GRACE_PERIOD,
        side_effect="delays suspension",
        reversibility=Reversibility.REVERSIBLE,
        base_tier=AutonomyTier.T2,
        never_below_base_tier=False,
        moves_money=False,
        customer_visible=True,
        is_outbound_contact=False,
        requires_consent=False,
        requires_quiet_hours_check=False,
        counts_toward_frequency_cap=False,
        requires_valid_mandate=False,
        requires_reconciliation_check=True,
        policy_categories=frozenset({_FINANCE, _HOLDS, _INTEGRITY}),
        guard_summary="max days by segment.",
    ),
    ActionType.INITIATE_VOICE_CALL: ActionSpec(
        action_type=ActionType.INITIATE_VOICE_CALL,
        side_effect="customer called",
        reversibility=Reversibility.IRREVERSIBLE,
        base_tier=AutonomyTier.T2,
        never_below_base_tier=True,
        moves_money=False,
        customer_visible=True,
        is_outbound_contact=True,
        channel_field="channel",
        requires_consent=True,
        requires_quiet_hours_check=True,
        counts_toward_frequency_cap=True,
        requires_valid_mandate=False,
        requires_reconciliation_check=True,
        policy_categories=_CONTACT_CATEGORIES,
        guard_summary="consent + quiet hours + automated-call disclosure + content "
        "check + recording notice. Human approval always.",
    ),
    ActionType.SUPPRESS_CONTACT: ActionSpec(
        action_type=ActionType.SUPPRESS_CONTACT,
        side_effect="prevents outreach",
        reversibility=Reversibility.REVERSIBLE,
        base_tier=AutonomyTier.T0,
        never_below_base_tier=False,
        moves_money=False,
        customer_visible=False,
        is_outbound_contact=False,
        requires_consent=False,
        requires_quiet_hours_check=False,
        counts_toward_frequency_cap=False,
        requires_valid_mandate=False,
        requires_reconciliation_check=False,
        policy_categories=frozenset({_INTEGRITY}),
        guard_summary="reason must reference an incident or a hold.",
    ),
    ActionType.OPEN_SYSTEMIC_INCIDENT: ActionSpec(
        action_type=ActionType.OPEN_SYSTEMIC_INCIDENT,
        side_effect="internal ticket",
        reversibility=Reversibility.REVERSIBLE,
        base_tier=AutonomyTier.T0,
        never_below_base_tier=False,
        moves_money=False,
        customer_visible=False,
        is_outbound_contact=False,
        requires_consent=False,
        requires_quiet_hours_check=False,
        counts_toward_frequency_cap=False,
        requires_valid_mandate=False,
        requires_reconciliation_check=False,
        policy_categories=frozenset({_INTEGRITY}),
        guard_summary="dedupe by cohort.",
    ),
    ActionType.PROPOSE_ROUTE_CHANGE: ActionSpec(
        action_type=ActionType.PROPOSE_ROUTE_CHANGE,
        side_effect="config change proposal",
        reversibility=Reversibility.REVERSIBLE,
        base_tier=AutonomyTier.T2,
        never_below_base_tier=True,
        moves_money=False,
        customer_visible=False,
        is_outbound_contact=False,
        requires_consent=False,
        requires_quiet_hours_check=False,
        counts_toward_frequency_cap=False,
        requires_valid_mandate=False,
        requires_reconciliation_check=False,
        is_config_change=True,
        policy_categories=frozenset({_INTEGRITY, _FINANCE}),
        guard_summary="never auto; diff + rollback plan required.",
    ),
    ActionType.ESCALATE_TO_HUMAN: ActionSpec(
        action_type=ActionType.ESCALATE_TO_HUMAN,
        side_effect="human task",
        reversibility=Reversibility.REVERSIBLE,
        base_tier=AutonomyTier.T0,
        never_below_base_tier=False,
        moves_money=False,
        customer_visible=False,
        is_outbound_contact=False,
        requires_consent=False,
        requires_quiet_hours_check=False,
        counts_toward_frequency_cap=False,
        requires_valid_mandate=False,
        requires_reconciliation_check=False,
        always_permitted=True,
        policy_categories=frozenset({_INTEGRITY}),
        guard_summary="always allowed -- the safe default.",
    ),
    ActionType.RECOMMEND_WRITE_OFF: ActionSpec(
        action_type=ActionType.RECOMMEND_WRITE_OFF,
        side_effect="recommendation only",
        reversibility=Reversibility.REVERSIBLE,
        base_tier=AutonomyTier.T3,
        never_below_base_tier=True,
        moves_money=False,
        customer_visible=False,
        is_outbound_contact=False,
        requires_consent=False,
        requires_quiet_hours_check=False,
        counts_toward_frequency_cap=False,
        requires_valid_mandate=False,
        requires_reconciliation_check=False,
        is_recommendation_only=True,
        policy_categories=frozenset({_FINANCE, _INTEGRITY}),
        guard_summary="agent can never execute; a human decides and another system acts.",
    ),
}


# --------------------------------------------------------- import-time guards

_forbidden_in_catalog = FORBIDDEN_VERBS & {t.value for t in ActionType}
if _forbidden_in_catalog:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "SECURITY: forbidden verbs present in the action catalog: "
        + ", ".join(sorted(_forbidden_in_catalog))
    )

_missing_models = set(ActionType) - set(ACTION_MODELS)
_missing_specs = set(ActionType) - set(ACTION_SPECS)
if _missing_models or _missing_specs:  # pragma: no cover - import-time guard
    raise RuntimeError(
        f"action catalog incomplete: models missing {sorted(t.value for t in _missing_models)}, "
        f"specs missing {sorted(t.value for t in _missing_specs)}"
    )

for _t, _m in ACTION_MODELS.items():  # pragma: no cover - import-time guard
    _declared = _m.model_fields["action_type"].default
    if _declared is not _t:
        raise RuntimeError(
            f"{_m.__name__}.action_type is {_declared!r} but it is registered under {_t!r}"
        )

for _t, _s in ACTION_SPECS.items():  # pragma: no cover - import-time guard
    _cf = _s.channel_field
    if _cf is not None and _cf not in ACTION_MODELS[_t].model_fields:
        raise RuntimeError(
            f"{_t.value} declares channel_field={_cf!r} but "
            f"{ACTION_MODELS[_t].__name__} has no such field. Every §14.1 contact "
            f"gate -- consent, quiet hours, frequency cap -- reads the channel "
            f"through that name, so this is an AttributeError on the compliance "
            f"path rather than a missing attribute on a spare field"
        )


# ------------------------------------------------------------- idempotency


def action_spec(action_type: ActionType) -> ActionSpec:
    return ACTION_SPECS[action_type]


def idempotency_key(action: _ActionBase, case_id: str) -> str:
    """Derive the idempotency key for one logical action (invariants #1 and #8).

    The key is ``<action_type>:<sha256 of the canonical scope>``. The prefix is for
    human readability in the ledger; the digest is the guarantee. Because the scope
    is canonical JSON, an LLM that re-proposes the same action with its fields in a
    different order produces the *same* key and the executor deduplicates it.

    CONTRACT DECISION (JC-19) -- what the key does NOT protect
    ----------------------------------------------------------
    ``case_id`` is part of the scope for every action. Two *different* cases on the
    same obligation therefore derive different keys, so the key alone does not
    prevent a double debit across cases. That protection is a uniqueness constraint
    on ``(obligation_id, attempt_sequence)`` in the execution store, and it belongs
    to Phase 1. Flagged for review: if you would rather the key itself carry it,
    ``ScheduleDebit`` must drop ``case_id`` from its scope, which then makes two
    legitimately distinct cases on one obligation collide.
    """
    scope = action.idempotency_scope(case_id)
    return f"{action.action_type.value}:{digest(scope)}"


class ActionEnvelope(BaseModel):
    """A proposed action with its provenance and its derived idempotency key.

    The key is a computed field: there is no way for a caller -- least of all the
    planner -- to supply one, so invariant #8 cannot be talked out of.

    A serialised envelope *does* carry the key, because the audit log stores it and
    §15 decision replay reads it back. On the way in, a claimed key is therefore
    accepted only when it equals the key derived from the parameters. That makes the
    stored key a tamper check rather than a second source of truth: edit the
    parameters of a written row and it will no longer parse.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: ActionId
    case_id: CaseId
    action: Action
    proposed_by: PlanOrigin
    catalog_version: str = ACTION_CATALOG_VERSION

    @model_validator(mode="wrap")
    @classmethod
    def _claimed_key_must_agree(cls, data: Any, handler: Any) -> "ActionEnvelope":
        claimed: str | None = None
        if isinstance(data, dict) and "idempotency_key" in data:
            data = dict(data)
            claimed = data.pop("idempotency_key")
        envelope = handler(data)
        if claimed is not None and claimed != envelope.idempotency_key:
            raise ValueError(
                "idempotency_key does not match the action parameters: this row was "
                f"either tampered with or written by a different catalog version. "
                f"claimed={claimed!r} derived={envelope.idempotency_key!r}"
            )
        return envelope

    @computed_field  # type: ignore[prop-decorator]
    @property
    def idempotency_key(self) -> str:
        return idempotency_key(self.action, self.case_id)

    @property
    def spec(self) -> ActionSpec:
        return ACTION_SPECS[self.action.action_type]


# ------------------------------------------------------------ LLM tool schemas


def tool_schemas_for_llm() -> list[dict[str, Any]]:
    """Tool definitions for the planner's model client.

    Generated from the same models the executor validates against, so the schema
    the model is shown and the schema it is held to cannot drift apart.
    """
    schemas: list[dict[str, Any]] = []
    for action_type, model in ACTION_MODELS.items():
        spec = ACTION_SPECS[action_type]
        schemas.append(
            {
                "name": action_type.value,
                "description": (
                    f"{spec.side_effect}. Reversibility: {spec.reversibility.value}. "
                    f"Minimum autonomy tier: {spec.base_tier.value}. "
                    f"Guards enforced after you propose it: {spec.guard_summary}"
                ),
                "input_schema": model.model_json_schema(),
            }
        )
    return schemas
