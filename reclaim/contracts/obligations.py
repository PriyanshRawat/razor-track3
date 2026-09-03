"""Obligation, party, mandate and hold schemas.

The **obligation** is the unit of everything: the Revenue-at-Risk Ledger holds one
row per obligation (§4), amount-at-risk is recognised once per obligation (§13),
and the randomisation unit is the obligation-case (§12.1).

Key contracts encoded here
--------------------------
* ``Obligation.outstanding`` -- the single definition of "what is owed". Invariant
  #6 ("total recovered per obligation <= amount owed") is checked against it.
* ``MandateStatus.is_debitable`` -- fail closed; only ACTIVE debits.
* ``ConsentProfile`` is **Optional at every call site**, and ``has_consent()``
  returns False for ``None``. §14.1: "absent consent record => no contact".
* ``SystemicIncident.member_case_ids`` -- an incident's at-risk amount is the sum
  of its members and is *not* additive on top (§13 anti-double-counting rule).
"""

from __future__ import annotations

from typing import Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reclaim.contracts.enums import (
    HOLD_STOP_REASONS,
    Channel,
    HumanQueue,
    Language,
    MandateStatus,
    ObligationKind,
    ObligationStatus,
    PspId,
    Rail,
    Segment,
    StopReason,
    SuppressionScope,
)
from reclaim.contracts.ids import (
    CaseId,
    CohortId,
    DocumentId,
    HoldId,
    IncidentId,
    MandateId,
    ObligationId,
    PayerId,
    SubscriptionId,
)
from reclaim.contracts.money import Currency, Money, money_sum
from reclaim.contracts.temporal import UtcDatetime
from reclaim.contracts.units import PValue

__all__ = [
    "B2BInvoiceFields",
    "CohortKey",
    "ConsentProfile",
    "ConsentRecord",
    "CreditNote",
    "Hold",
    "HoldKind",
    "Mandate",
    "Obligation",
    "PartialPayment",
    "Payer",
    "QuietHours",
    "SystemicIncident",
    "has_consent",
]


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


class ConsentRecord(BaseModel):
    """Consent for one channel. Absence of a record is *not* the same as
    ``granted=False``; both block contact, but only the latter is auditable as a
    withdrawal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: Channel
    granted: bool
    granted_at: UtcDatetime | None = None
    withdrawn_at: UtcDatetime | None = None
    source: str = Field(description="Where consent was captured, e.g. 'signup_form_v3'.")
    dpdp_purpose: str = Field(
        description="DPDP purpose limitation: the declared purpose this consent "
        "covers. A contact for a different purpose is not covered (§14.1)."
    )

    @property
    def is_effective(self) -> bool:
        return self.granted and self.withdrawn_at is None


class QuietHours(BaseModel):
    """Local-time contact window. Default 09:00-19:00 IST (§14.1).

    Stored as local hours plus the payer's IANA zone; the policy engine converts.

    This type is the payer's *stated* window. It is not the only source -- the
    configured global default in ``PolicyThresholds`` is the other -- and which
    one governs is decided in exactly one place, ``policy_format.resolve_quiet_hours``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_hour_local: int = Field(default=9, ge=0, le=23)
    end_hour_local: int = Field(default=19, ge=1, le=24)
    timezone_name: str = Field(default="Asia/Kolkata")

    @model_validator(mode="after")
    def _ordered(self) -> "QuietHours":
        if self.end_hour_local <= self.start_hour_local:
            raise ValueError("end_hour_local must be after start_hour_local")
        return self


class ConsentProfile(BaseModel):
    """Per-payer consent and preference store (§10.1 ``get_consent_profile``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payer_id: PayerId
    records: tuple[ConsentRecord, ...] = ()
    language: Language = Language.EN_IN
    quiet_hours: QuietHours | None = Field(
        default=None,
        description="The payer's *stated* contact window, or None when they have "
        "not stated one. Optional rather than defaulted (CONTRACTS.md §7 N7): a "
        "default here is indistinguishable from a preference, so the global "
        "configured window in PolicyThresholds could never legitimately apply and "
        "the two sources had no precedence between them. Never read this field "
        "directly -- policy_format.resolve_quiet_hours is where the precedence "
        "rule lives, and a second reading of it is a second rule.",
    )
    on_dnc_list: bool = False
    updated_at: UtcDatetime | None = None

    def record_for(self, channel: Channel) -> ConsentRecord | None:
        """The record governing ``channel``, or None.

        Returns the *first* match, which is unambiguous only because
        ``_one_record_per_channel`` below forbids a second.
        """
        for rec in self.records:
            if rec.channel is channel:
                return rec
        return None

    @model_validator(mode="after")
    def _one_record_per_channel(self) -> "ConsentProfile":
        """A channel may appear once. Two records make consent order-dependent.

        ``record_for`` takes the first match, so a profile holding a withdrawn
        record and a live one for the same channel answered ``has_consent``
        differently depending on which was listed first -- on the gate that
        enforces "no contact after opt-out" (§14.1, invariant #2). Withdrawal is
        already modelled *inside* a record (``granted`` plus ``withdrawn_at``), so
        a second row is not a history, it is a contradiction with a tie-break.

        Rejected at construction rather than resolved by picking the most
        restrictive: a profile that disagrees with itself is a bug in whatever
        assembled it, and silently choosing one row would hide that from the
        audit trail while still contacting -- or not contacting -- someone.
        """
        channels = [rec.channel for rec in self.records]
        duplicates = sorted({c.value for c in channels if channels.count(c) > 1})
        if duplicates:
            raise ValueError(
                f"consent profile for {self.payer_id} carries more than one record "
                f"for {duplicates}; consent would depend on record order"
            )
        return self


def has_consent(profile: Optional[ConsentProfile], channel: Channel) -> bool:
    """§14.1: absent consent record => no contact. §10.1: profile unavailable =>
    treat as no consent. Both collapse to False here, deliberately."""
    if profile is None or profile.on_dnc_list:
        return False
    record = profile.record_for(channel)
    return record is not None and record.is_effective


# ---------------------------------------------------------------------------
# Holds
# ---------------------------------------------------------------------------


class HoldKind(BaseModel):
    """Superseded by ``enums.HOLD_STOP_REASONS``; retained only as a name.

    This existed to "keep hold kinds enumerable without a second enum import"
    while ``Hold.kind`` was a free string. ``HOLD_STOP_REASONS`` now makes them
    genuinely enumerable, so nothing should reach for this. Kept rather than
    deleted because removing an exported name is a MAJOR break for no gain; it is
    a candidate for deletion at the next one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str


class Hold(BaseModel):
    """An immediate hard stop (§14.1 Holds, §14.3).

    A hold is a first-class row rather than a boolean on the payer so that it
    carries provenance and an auditable open/close time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hold_id: HoldId
    payer_id: PayerId
    obligation_id: ObligationId | None = None
    kind: StopReason = Field(
        description="Which of §14.1's seven immediate hard stops this is. Typed "
        "as StopReason, restricted to HOLD_STOP_REASONS: a hold and the stop it "
        "causes must name the same thing, or §12.4's stop-reason breakdown counts "
        "two vocabularies for one event."
    )
    opened_at: UtcDatetime
    released_at: UtcDatetime | None = None
    reason: str
    opened_by: str
    routed_to: HumanQueue | None = None

    @field_validator("kind")
    @classmethod
    def _kind_is_a_hold_not_a_ladder_stop(cls, value: StopReason) -> StopReason:
        """Only the seven §14.1 hold reasons may open a hold.

        ``StopReason`` also carries reasons that end a *ladder* -- a contact cap, an
        approval timeout, an already-paid reconciliation. Those stop one case; a
        hold suppresses the payer. Accepting one here would suppress contact that
        policy still permits, and it is the kind of over-blocking that looks like
        caution until someone asks why a paying customer heard nothing.
        """
        if value not in HOLD_STOP_REASONS:
            raise ValueError(
                f"{value.value!r} is a stop reason but not a hold; §14.1's holds "
                f"are {sorted(r.value for r in HOLD_STOP_REASONS)}"
            )
        return value

    @property
    def is_active(self) -> bool:
        return self.released_at is None


# ---------------------------------------------------------------------------
# Parties
# ---------------------------------------------------------------------------


class Payer(BaseModel):
    """A paying party. Deliberately carries **no PAN and no raw contact details**
    (§10.3 data minimisation) -- channel addresses live in the comms adapter, keyed
    by ``payer_id``, behind the redaction layer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payer_id: PayerId
    segment: Segment
    timezone_name: str = "Asia/Kolkata"
    preferred_language: Language = Language.EN_IN
    created_at: UtcDatetime
    display_name_ref: str | None = Field(
        default=None,
        description="Opaque reference to the name in the redaction-protected store. "
        "Never the name itself, so prompts cannot carry it by accident.",
    )
    account_owner_ref: str | None = Field(
        default=None, description="Internal CSM/AE reference for B2B accounts."
    )


# ---------------------------------------------------------------------------
# Mandates
# ---------------------------------------------------------------------------


class Mandate(BaseModel):
    """An e-mandate / autopay authorisation.

    ``immutable=True`` is not a flag we set -- it records a property of the rail:
    Stripe's India docs state "You can't cancel or update a mandate." Recovery from
    a dead mandate is therefore a *new registration journey*, which is why
    ``create_mandate_reauth_link`` exists in the action catalog and
    ``update_mandate`` does not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mandate_id: MandateId
    payer_id: PayerId
    psp: PspId
    rail: Rail
    status: MandateStatus
    cap: Money = Field(description="Maximum debitable amount per transaction.")
    registered_at: UtcDatetime | None = None
    afa_completed_at: UtcDatetime | None = Field(
        default=None,
        description="When additional-factor authentication was last completed. "
        "Above the AFA threshold RBI requires AFA on *every* debit, so this is a "
        "per-attempt requirement, not a one-off.",
    )
    revoked_at: UtcDatetime | None = None
    pause_reason: str | None = None
    immutable: bool = Field(
        default=True,
        description="True for India e-mandate rails: no API cancel or update.",
    )
    subscription_id: SubscriptionId | None = None

    @property
    def is_debitable(self) -> bool:
        """Fail closed. UNKNOWN or any non-ACTIVE status forbids a debit."""
        return self.status.is_debitable and self.revoked_at is None

    def permits_amount(self, amount: Money) -> bool:
        """Invariant #5: no debit exceeding the mandate cap."""
        return amount <= self.cap


# ---------------------------------------------------------------------------
# Obligations
# ---------------------------------------------------------------------------


class PartialPayment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Money
    received_at: UtcDatetime
    match_method: Literal["bank_feed", "psp_settlement", "manual"] = Field(
        description="How the money was matched to this obligation. The same closed "
        "vocabulary as PaymentReceivedPayload.match_method, which is the event that "
        "produces this row -- §14.4's 'payment status is never read from a message' "
        "has to hold on the ledger row, not only on the event."
    )
    reference: str | None = None


class CreditNote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Money
    issued_at: UtcDatetime
    reason: str
    document_id: DocumentId | None = None


class B2BInvoiceFields(BaseModel):
    """Fields whose absence or mismatch is the actual blocker in B2B
    non-payment (§9.2 H8). Modelled explicitly so a process defect is a *typed*
    finding, not a sentence in a narrative."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    po_reference: str | None = None
    buyer_requires_po: bool | None = None
    gstin: str | None = None
    buyer_gstin: str | None = None
    invoice_format: str | None = None
    buyer_required_format: str | None = None
    approver_ref: str | None = None
    portal_submission_ref: str | None = None

    @property
    def has_detectable_defect(self) -> bool:
        """Deterministic pre-check. The LLM explains *why*; this says *whether*."""
        if self.buyer_requires_po and not self.po_reference:
            return True
        if (
            self.buyer_required_format
            and self.invoice_format
            and self.buyer_required_format != self.invoice_format
        ):
            return True
        if self.buyer_gstin and not self.gstin:
            return True
        return False


class Obligation(BaseModel):
    """One thing that is owed. The atomic unit of the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    obligation_id: ObligationId
    kind: ObligationKind
    payer_id: PayerId
    currency: Currency = Currency.INR
    gross_amount: Money
    issued_at: UtcDatetime
    due_at: UtcDatetime
    status: ObligationStatus = ObligationStatus.OPEN
    partial_payments: tuple[PartialPayment, ...] = ()
    credit_notes: tuple[CreditNote, ...] = ()
    subscription_id: SubscriptionId | None = None
    mandate_id: MandateId | None = None
    billing_period_start: UtcDatetime | None = None
    billing_period_end: UtcDatetime | None = None
    b2b_fields: B2BInvoiceFields | None = None

    @property
    def paid_amount(self) -> Money:
        return money_sum((p.amount for p in self.partial_payments), self.currency)

    @property
    def credited_amount(self) -> Money:
        return money_sum((c.amount for c in self.credit_notes), self.currency)

    @property
    def outstanding(self) -> Money:
        """The single definition of "what is owed". Floored at zero.

        Flooring is deliberate: an over-payment is a reconciliation problem, not a
        negative obligation, and a negative outstanding would let the value model
        chase a refund as if it were recovery.
        """
        return (self.gross_amount - self.paid_amount - self.credited_amount).clamp_at_least_zero()

    @property
    def is_fully_settled(self) -> bool:
        return self.outstanding.is_zero

    def aging_days_at(self, at: UtcDatetime) -> int:  # type: ignore[valid-type]
        """Whole days past due at ``at``. Negative before the due date."""
        return (at - self.due_at).days  # type: ignore[operator]

    @model_validator(mode="after")
    def _currency_consistency(self) -> "Obligation":
        if self.gross_amount.currency is not self.currency:
            raise ValueError("gross_amount currency must match obligation currency")
        for p in self.partial_payments:
            if p.amount.currency is not self.currency:
                raise ValueError("partial payment currency mismatch")
        for c in self.credit_notes:
            if c.amount.currency is not self.currency:
                raise ValueError("credit note currency mismatch")
        return self

    @model_validator(mode="after")
    def _recovered_not_exceeding_owed(self) -> "Obligation":
        """Runtime invariant #6, enforced at the schema boundary.

        A tolerance is *not* applied: paise are exact. An over-collection is a real
        defect and must surface here rather than in the scoreboard.
        """
        collected = self.paid_amount + self.credited_amount
        if collected > self.gross_amount:
            raise ValueError(
                f"Invariant #6 violated for {self.obligation_id}: collected "
                f"{collected} exceeds gross {self.gross_amount}"
            )
        return self


# ---------------------------------------------------------------------------
# Systemic incidents
# ---------------------------------------------------------------------------


class CohortKey(BaseModel):
    """The dimensions along which D5 tests for auth-rate degradation (§4).

    Frozen and canonicalised so ``open_systemic_incident`` can dedupe by cohort
    (§10.2 guard) without a fuzzy match.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    psp: PspId | None = None
    issuer: str | None = None
    bin_range: str | None = None
    rail: Rail | None = None
    route: str | None = None
    window_start: UtcDatetime | None = None
    window_end: UtcDatetime | None = None

    @property
    def dedupe_key(self) -> str:
        """Canonical string over the *non-temporal* dimensions.

        The time window is excluded on purpose: two detections of the same
        issuer x BIN x route degradation twenty minutes apart are one incident, not
        two, and deduping on a window would defeat the guard.
        """
        parts = [
            f"psp={self.psp.value if self.psp else '*'}",
            f"issuer={self.issuer or '*'}",
            f"bin={self.bin_range or '*'}",
            f"rail={self.rail.value if self.rail else '*'}",
            f"route={self.route or '*'}",
        ]
        return "|".join(parts)


class SystemicIncident(BaseModel):
    """An our-side or cohort-wide failure (§9.2 H6).

    Anti-double-counting (§13): ``amount_at_risk`` for an incident is the sum of
    its member cases. It is **not** an additional at-risk row. ``counts_toward_at_risk``
    on the member ``RiskCase`` rows stays True; the incident row itself never
    contributes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: IncidentId
    cohort_id: CohortId | None = None
    cohort_key: CohortKey
    hypothesis: str
    opened_at: UtcDatetime
    resolved_at: UtcDatetime | None = None
    attributable_to_us: bool = Field(
        description="True => suppress the whole cohort's customer contact and count "
        "each suppressed contact as an avoided false action (§13, §17)."
    )
    member_case_ids: tuple[CaseId, ...] = ()
    detected_by_detector: str = "D5"
    suppression_scope: SuppressionScope = SuppressionScope.COHORT
    fdr_adjusted_p_value: PValue | None = Field(
        default=None,
        description="Benjamini-Hochberg adjusted p-value from the cohort test. "
        "Recorded so the false-alarm rate is auditable (§12.3). A quantised "
        "Decimal, not a float: this field reaches the audit chain, and "
        "canonical_json rejects floats (JC-15).",
    )

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    @property
    def counts_toward_at_risk(self) -> bool:
        """Always False. Stated as a property so the metrics module can assert it."""
        return False


#: Convenience alias used by the metrics module's docstrings.
INCIDENT_AT_RISK_IS_NOT_ADDITIVE: Mapping[str, str] = {
    "rule": "A systemic incident's at-risk equals the sum of its member cases; it "
    "is not additive on top of them (§13).",
}
