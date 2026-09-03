"""Canonical event envelope and typed ingest payloads.

Every fact that enters RECLAIM enters as a ``CanonicalEvent``. Nothing downstream
reads a PSP webhook, a CSV row, or an inbound message directly.

Three contracts are frozen here
-------------------------------
1. **Idempotent ingest.** ``(source_system, source_event_id)`` is the uniqueness
   key. A PSP that redelivers a webhook three times produces one event.
   Invariant #1 ("no duplicate side effects") starts at ingest, not at execution.
2. **Raw stays raw.** The payload carries the PSP's ``raw_decline_code`` verbatim.
   Normalisation to a ``DeclineClass`` happens *downstream* and is recorded on the
   case with its taxonomy version. If we normalised at ingest we could never
   re-map a mis-classified code without rewriting history.
3. **Trust is a field, not an assumption.** ``TrustLevel`` travels with the
   content. Customer-supplied text is ``CUSTOMER_SUPPLIED_UNTRUSTED`` and its body
   is stored **by reference**, never inline in the envelope -- so an event cannot
   be pasted into an instruction channel by accident (§14.5).

CONTRACT DECISION (JC-13): payloads form a discriminated union on a literal
``kind`` field *and* are registered in ``PAYLOAD_MODELS``. The union gives
validation; the registry gives an import-time exhaustiveness check so a new
``EventType`` cannot be added without a payload model.

CONTRACT DECISION (JC-14): ``MessageInboundPayload.untrusted`` is
``Literal[True]``. It is not settable to False. A code path that wants to treat an
inbound message as trusted has to change this contract, in a diff, under review.
"""

from __future__ import annotations

from typing import Annotated, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reclaim.contracts.enums import (
    Channel,
    Language,
    MandateStatus,
    ObligationKind,
    ObligationStatus,
    PspId,
    Rail,
    Segment,
)
from reclaim.contracts.ids import (
    AttemptId,
    CaseId,
    DocumentId,
    LinkId,
    MandateId,
    MessageId,
    ObligationId,
    PayerId,
    SubscriptionId,
)
from reclaim.contracts.money import Money
from reclaim.contracts.obligations import B2BInvoiceFields, CohortKey
from reclaim.contracts.temporal import UtcDatetime
from reclaim.contracts.versions import EVENT_SCHEMA_VERSION

try:  # pragma: no cover - 3.11+
    from enum import StrEnum
except ImportError:  # pragma: no cover
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """Minimal StrEnum shim."""


__all__ = [
    "AttemptOutcome",
    "AuthRateSnapshotPayload",
    "CanonicalEvent",
    "ChargebackOpenedPayload",
    "CheckoutAbandonedPayload",
    "ClockTickPayload",
    "CreditNoteIssuedPayload",
    "DeliveryStatus",
    "EVENT_PAYLOAD_UNION_MEMBERS",
    "EventPayload",
    "EventType",
    "HumanDecisionPayload",
    "LinkInteractionPayload",
    "MandateStateChangedPayload",
    "MessageDeliveryStatusPayload",
    "MessageInboundPayload",
    "ObligationCreatedPayload",
    "ObligationUpdatedPayload",
    "PAYLOAD_MODELS",
    "PaymentAttemptPayload",
    "PaymentReceivedPayload",
    "SubscriptionCancelledPayload",
    "TrustLevel",
]

#: Longest redacted preview permitted in an event envelope. Long enough to render
#: in the ledger UI, short enough that the envelope is not a smuggling channel.
MAX_REDACTED_PREVIEW_CHARS = 240


class TrustLevel(StrEnum):
    """Provenance of a piece of content. Determines where it may appear.

    ``CUSTOMER_SUPPLIED_UNTRUSTED`` content may appear in a *data* channel given to
    the LLM (clearly delimited, per §14.5) and in the human UI. It may never appear
    in a system prompt, a tool name, or an action parameter that a policy rule
    reads for authority.
    """

    SYSTEM_OF_RECORD = "system_of_record"
    PSP_VERIFIED = "psp_verified"
    BANK_FEED = "bank_feed"
    CUSTOMER_SUPPLIED_UNTRUSTED = "customer_supplied_untrusted"
    LLM_DERIVED = "llm_derived"
    HUMAN_ASSERTED = "human_asserted"

    @property
    def is_authoritative_for_payment_state(self) -> bool:
        """§14.4: payment status comes from the ledger or the bank feed only.
        A customer saying "I paid" is a claim to verify, never a state change.
        """
        return self in (
            TrustLevel.SYSTEM_OF_RECORD,
            TrustLevel.PSP_VERIFIED,
            TrustLevel.BANK_FEED,
        )


class EventType(StrEnum):
    """Closed set of ingestable facts. One payload model each."""

    OBLIGATION_CREATED = "obligation_created"
    OBLIGATION_UPDATED = "obligation_updated"
    PAYMENT_ATTEMPT = "payment_attempt"
    PAYMENT_RECEIVED = "payment_received"
    CREDIT_NOTE_ISSUED = "credit_note_issued"
    MANDATE_STATE_CHANGED = "mandate_state_changed"
    CHECKOUT_ABANDONED = "checkout_abandoned"
    MESSAGE_INBOUND = "message_inbound"
    MESSAGE_DELIVERY_STATUS = "message_delivery_status"
    LINK_INTERACTION = "link_interaction"
    CHARGEBACK_OPENED = "chargeback_opened"
    SUBSCRIPTION_CANCELLED = "subscription_cancelled"
    AUTH_RATE_SNAPSHOT = "auth_rate_snapshot"
    HUMAN_DECISION = "human_decision"
    CLOCK_TICK = "clock_tick"


class AttemptOutcome(StrEnum):
    """Outcome of a debit attempt.

    ``TIMEOUT`` and ``UNKNOWN`` are distinct from ``FAILED``: an unknown outcome
    must **not** be retried, because the first attempt may have succeeded (§16
    "reconcile before assuming failure").
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PENDING = "pending"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"

    @property
    def is_safe_to_retry_without_reconciliation(self) -> bool:
        return self is AttemptOutcome.FAILED


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    BOUNCED = "bounced"
    FAILED = "failed"
    SPAM_COMPLAINT = "spam_complaint"
    UNSUBSCRIBED = "unsubscribed"

    @property
    def is_negative_signal(self) -> bool:
        """Feeds the complaint-rate guardrail (§13) and the channel-health rule."""
        return self in (
            DeliveryStatus.BOUNCED,
            DeliveryStatus.SPAM_COMPLAINT,
            DeliveryStatus.UNSUBSCRIBED,
        )


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


class _Payload(BaseModel):
    """Base for all payloads: frozen, closed, and never carrying raw PII inline."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ObligationCreatedPayload(_Payload):
    kind: Literal["obligation_created"] = "obligation_created"

    obligation_id: ObligationId
    payer_id: PayerId
    obligation_kind: ObligationKind
    gross_amount: Money
    issued_at: UtcDatetime
    due_at: UtcDatetime
    segment: Segment
    subscription_id: SubscriptionId | None = None
    mandate_id: MandateId | None = None
    b2b_fields: B2BInvoiceFields | None = None


class ObligationUpdatedPayload(_Payload):
    kind: Literal["obligation_updated"] = "obligation_updated"

    obligation_id: ObligationId
    new_status: ObligationStatus | None = None
    new_due_at: UtcDatetime | None = None
    new_gross_amount: Money | None = None
    b2b_fields: B2BInvoiceFields | None = None
    reason: str | None = None


class PaymentAttemptPayload(_Payload):
    """A debit attempt on a rail. The only source of decline codes.

    ``raw_decline_code`` is **not** normalised here on purpose (see module
    docstring, contract 2). ``network_advice_code`` is separate because the card
    networks' "do not retry" advice is authoritative independently of the PSP's own
    decline string.
    """

    kind: Literal["payment_attempt"] = "payment_attempt"

    attempt_id: AttemptId
    obligation_id: ObligationId
    payer_id: PayerId
    psp: PspId
    rail: Rail
    amount: Money
    outcome: AttemptOutcome
    attempted_at: UtcDatetime
    mandate_id: MandateId | None = None
    raw_decline_code: str | None = Field(
        default=None,
        description="Verbatim PSP code. Normalised downstream against the "
        "taxonomy version recorded on the case.",
    )
    raw_decline_message_ref: DocumentId | None = Field(
        default=None,
        description="Reference to the PSP's human-readable message. Stored by "
        "reference because issuer messages occasionally contain payer data.",
    )
    network_advice_code: str | None = None
    is_customer_present: bool = False
    initiated_by_reclaim: bool = Field(
        default=False,
        description="True when RECLAIM initiated the attempt. Attribution (§13) "
        "requires distinguishing our retries from the billing system's.",
    )
    afa_performed: bool | None = None
    settled_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _decline_code_only_on_failure(self) -> "PaymentAttemptPayload":
        if self.outcome is AttemptOutcome.SUCCEEDED and self.raw_decline_code:
            raise ValueError("a succeeded attempt cannot carry a decline code")
        return self


class PaymentReceivedPayload(_Payload):
    """Money actually arrived. The authoritative recovery signal (§14.4)."""

    kind: Literal["payment_received"] = "payment_received"

    obligation_id: ObligationId
    payer_id: PayerId
    amount: Money
    received_at: UtcDatetime
    match_method: Literal["bank_feed", "psp_settlement", "manual"]
    trust: TrustLevel = TrustLevel.BANK_FEED
    attempt_id: AttemptId | None = None
    reference: str | None = None

    @field_validator("trust")
    @classmethod
    def _must_be_authoritative(cls, v: TrustLevel) -> TrustLevel:
        if not v.is_authoritative_for_payment_state:
            raise ValueError(
                "payment_received requires an authoritative trust level; a "
                "customer claim of payment is a MESSAGE_INBOUND event, not this one"
            )
        return v


class CreditNoteIssuedPayload(_Payload):
    kind: Literal["credit_note_issued"] = "credit_note_issued"

    obligation_id: ObligationId
    amount: Money
    issued_at: UtcDatetime
    reason: str
    document_id: DocumentId | None = None


class MandateStateChangedPayload(_Payload):
    kind: Literal["mandate_state_changed"] = "mandate_state_changed"

    mandate_id: MandateId
    payer_id: PayerId
    psp: PspId
    rail: Rail
    new_status: MandateStatus
    changed_at: UtcDatetime
    cap: Money | None = None
    reason: str | None = None
    afa_completed_at: UtcDatetime | None = None


class CheckoutAbandonedPayload(_Payload):
    """D4 input. ``failure_stage`` distinguishes "changed their mind" from
    "our checkout broke" -- the two demand opposite actions (§9.2 H6)."""

    kind: Literal["checkout_abandoned"] = "checkout_abandoned"

    obligation_id: ObligationId
    payer_id: PayerId
    cart_amount: Money
    abandoned_at: UtcDatetime
    failure_stage: Literal[
        "pre_payment", "payment_form", "authentication", "post_auth_error", "unknown"
    ] = "unknown"
    psp: PspId | None = None
    rail: Rail | None = None
    raw_error_code: str | None = None


class MessageInboundPayload(_Payload):
    """A customer reply. **Untrusted by construction** (JC-14).

    The body is not in the envelope. Only a redacted preview is, capped at
    ``MAX_REDACTED_PREVIEW_CHARS``, for the human ledger view. Intent extraction
    reads the body from the document store through the redaction layer, and its
    output is an ``ExtractedIntent`` -- a claim, not a fact.
    """

    kind: Literal["message_inbound"] = "message_inbound"

    message_id: MessageId
    payer_id: PayerId
    channel: Channel
    received_at: UtcDatetime
    body_ref: DocumentId = Field(
        description="Reference to the stored body. The body itself never enters "
        "this envelope, so an envelope cannot be interpolated into a prompt."
    )
    redacted_preview: str = Field(
        default="",
        max_length=MAX_REDACTED_PREVIEW_CHARS,
        description="Redacted, truncated preview for the human UI only.",
    )
    trust: Literal[TrustLevel.CUSTOMER_SUPPLIED_UNTRUSTED] = (
        TrustLevel.CUSTOMER_SUPPLIED_UNTRUSTED
    )
    untrusted: Literal[True] = True
    language_detected: Language | None = None
    in_reply_to_message_id: MessageId | None = None
    obligation_id: ObligationId | None = None
    has_attachments: bool = False


class MessageDeliveryStatusPayload(_Payload):
    kind: Literal["message_delivery_status"] = "message_delivery_status"

    message_id: MessageId
    payer_id: PayerId
    channel: Channel
    status: DeliveryStatus
    occurred_at: UtcDatetime
    provider_code: str | None = None


class LinkInteractionPayload(_Payload):
    """Behavioural signal from a hosted link (payment page, mandate re-auth).

    ``completed`` is the only field the policy engine trusts as evidence of
    customer action; ``opened`` is a weak signal and explicitly may not be used to
    infer payment (§14.4).
    """

    kind: Literal["link_interaction"] = "link_interaction"

    link_id: LinkId
    payer_id: PayerId
    obligation_id: ObligationId | None = None
    interaction: Literal["created", "opened", "started", "completed", "expired", "failed"]
    occurred_at: UtcDatetime
    link_purpose: Literal[
        "payment", "credential_update", "mandate_reauth", "afa_completion", "invoice_view"
    ]


class ChargebackOpenedPayload(_Payload):
    kind: Literal["chargeback_opened"] = "chargeback_opened"

    obligation_id: ObligationId
    payer_id: PayerId
    amount: Money
    opened_at: UtcDatetime
    psp: PspId
    reason_code: str | None = None


class SubscriptionCancelledPayload(_Payload):
    kind: Literal["subscription_cancelled"] = "subscription_cancelled"

    subscription_id: SubscriptionId
    payer_id: PayerId
    cancelled_at: UtcDatetime
    initiated_by: Literal["customer", "merchant", "system"]
    reason_code: str | None = None


class AuthRateSnapshotPayload(_Payload):
    """Aggregate authorisation-rate observation for one cohort in one window.

    D5's input. Counts, not rates: the detector computes the rate so the
    denominator is auditable and the FDR correction has the sample size (§12.3).
    """

    kind: Literal["auth_rate_snapshot"] = "auth_rate_snapshot"

    cohort_key: CohortKey
    window_start: UtcDatetime
    window_end: UtcDatetime
    attempts: int = Field(ge=0)
    successes: int = Field(ge=0)
    baseline_attempts: int | None = Field(default=None, ge=0)
    baseline_successes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _counts_coherent(self) -> "AuthRateSnapshotPayload":
        if self.successes > self.attempts:
            raise ValueError("successes cannot exceed attempts")
        if (
            self.baseline_attempts is not None
            and self.baseline_successes is not None
            and self.baseline_successes > self.baseline_attempts
        ):
            raise ValueError("baseline_successes cannot exceed baseline_attempts")
        if self.window_end <= self.window_start:  # type: ignore[operator]
            raise ValueError("window_end must be after window_start")
        return self


class HumanDecisionPayload(_Payload):
    """An approval, rejection, edit or override from the human console (§17).

    ``actor_ref`` is an internal reference, never a name, so the audit chain does
    not become a personal-data store.
    """

    kind: Literal["human_decision"] = "human_decision"

    case_id: CaseId
    decision: Literal["approve", "reject", "edit", "escalate", "release_hold", "override"]
    decided_at: UtcDatetime
    actor_ref: str
    reason: str | None = None
    trust: Literal[TrustLevel.HUMAN_ASSERTED] = TrustLevel.HUMAN_ASSERTED


class ClockTickPayload(_Payload):
    """Time passing is an event.

    Aging, quiet-hour boundaries and promise deadlines are all triggered by ticks
    rather than by a wall-clock read inside business logic, so the whole pipeline
    is replayable at accelerated time (§20 simulator) with byte-identical results.
    """

    kind: Literal["clock_tick"] = "clock_tick"

    tick_at: UtcDatetime
    simulated: bool = False


EVENT_PAYLOAD_UNION_MEMBERS = (
    ObligationCreatedPayload,
    ObligationUpdatedPayload,
    PaymentAttemptPayload,
    PaymentReceivedPayload,
    CreditNoteIssuedPayload,
    MandateStateChangedPayload,
    CheckoutAbandonedPayload,
    MessageInboundPayload,
    MessageDeliveryStatusPayload,
    LinkInteractionPayload,
    ChargebackOpenedPayload,
    SubscriptionCancelledPayload,
    AuthRateSnapshotPayload,
    HumanDecisionPayload,
    ClockTickPayload,
)

EventPayload = Annotated[
    Union[
        ObligationCreatedPayload,
        ObligationUpdatedPayload,
        PaymentAttemptPayload,
        PaymentReceivedPayload,
        CreditNoteIssuedPayload,
        MandateStateChangedPayload,
        CheckoutAbandonedPayload,
        MessageInboundPayload,
        MessageDeliveryStatusPayload,
        LinkInteractionPayload,
        ChargebackOpenedPayload,
        SubscriptionCancelledPayload,
        AuthRateSnapshotPayload,
        HumanDecisionPayload,
        ClockTickPayload,
    ],
    Field(discriminator="kind"),
]

#: EventType -> payload model. Checked for exhaustiveness at import time.
PAYLOAD_MODELS: Mapping[EventType, type[_Payload]] = {
    EventType.OBLIGATION_CREATED: ObligationCreatedPayload,
    EventType.OBLIGATION_UPDATED: ObligationUpdatedPayload,
    EventType.PAYMENT_ATTEMPT: PaymentAttemptPayload,
    EventType.PAYMENT_RECEIVED: PaymentReceivedPayload,
    EventType.CREDIT_NOTE_ISSUED: CreditNoteIssuedPayload,
    EventType.MANDATE_STATE_CHANGED: MandateStateChangedPayload,
    EventType.CHECKOUT_ABANDONED: CheckoutAbandonedPayload,
    EventType.MESSAGE_INBOUND: MessageInboundPayload,
    EventType.MESSAGE_DELIVERY_STATUS: MessageDeliveryStatusPayload,
    EventType.LINK_INTERACTION: LinkInteractionPayload,
    EventType.CHARGEBACK_OPENED: ChargebackOpenedPayload,
    EventType.SUBSCRIPTION_CANCELLED: SubscriptionCancelledPayload,
    EventType.AUTH_RATE_SNAPSHOT: AuthRateSnapshotPayload,
    EventType.HUMAN_DECISION: HumanDecisionPayload,
    EventType.CLOCK_TICK: ClockTickPayload,
}

_missing_payloads = set(EventType) - set(PAYLOAD_MODELS)
if _missing_payloads:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "PAYLOAD_MODELS is missing entries for: "
        + ", ".join(sorted(e.value for e in _missing_payloads))
    )

_expected_kinds = {model.model_fields["kind"].default for model in PAYLOAD_MODELS.values()}
_declared_kinds = {e.value for e in EventType}
if _expected_kinds != _declared_kinds:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "Payload `kind` literals do not match EventType values: "
        f"{sorted(_expected_kinds ^ _declared_kinds)}"
    )


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class CanonicalEvent(BaseModel):
    """The single entry point for facts.

    ``occurred_at`` vs ``ingested_at`` are both required and both used:
    ``occurred_at`` orders the business timeline (and is what the hazard model
    consumes), ``ingested_at`` orders our processing. Confusing the two produces a
    timing model trained on our own latency.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: EventType
    payload: EventPayload
    occurred_at: UtcDatetime
    ingested_at: UtcDatetime
    source_system: str = Field(
        min_length=1,
        description="e.g. 'stripe_test', 'sim_psp_2', 'billing_csv', 'comms_sim'.",
    )
    source_event_id: str = Field(
        min_length=1,
        description="The source's own identifier. With source_system this forms the "
        "ingest idempotency key.",
    )
    schema_version: str = EVENT_SCHEMA_VERSION
    trust: TrustLevel = TrustLevel.SYSTEM_OF_RECORD
    correlation_id: str | None = Field(
        default=None,
        description="Set when this event was caused by a RECLAIM action, so effects "
        "can be attributed to the action that produced them (§13).",
    )

    @property
    def ingest_key(self) -> str:
        """Idempotency key for ingest. Duplicate webhooks collapse here."""
        return f"{self.source_system}:{self.source_event_id}"

    @model_validator(mode="after")
    def _payload_matches_type(self) -> "CanonicalEvent":
        expected = PAYLOAD_MODELS[self.event_type]
        if not isinstance(self.payload, expected):
            raise ValueError(
                f"event_type {self.event_type.value} requires "
                f"{expected.__name__}, got {type(self.payload).__name__}"
            )
        return self

    @model_validator(mode="after")
    def _untrusted_payload_forces_untrusted_envelope(self) -> "CanonicalEvent":
        """An untrusted payload cannot be laundered by a trusted envelope."""
        if isinstance(self.payload, MessageInboundPayload) and (
            self.trust is not TrustLevel.CUSTOMER_SUPPLIED_UNTRUSTED
        ):
            raise ValueError(
                "MESSAGE_INBOUND must carry trust=customer_supplied_untrusted"
            )
        return self
