"""Canonical decline-code taxonomy and retryability classes.

Scope of this file (Phase 0)
----------------------------
* ``DeclineClass``   -- the closed canonical vocabulary (frozen).
* ``Retryability``   -- what, mechanically, would have to change for a retry to
  have non-zero probability.
* ``DeclineClassMeta`` + ``DECLINE_CLASS_META`` -- per-class metadata the triage
  gate, policy engine and value model all read.
* ``DeclineCodeMapping`` -- the *format* of a PSP-code -> canonical-class row, plus
  the handful of seed rows that HACKATHON_PLAN.md cites verbatim so that golden
  tests exist from hour zero.

**Out of scope (Phase 1, §18.1 item 4):** the full per-PSP mapping table, the
message-pattern fallback logic, and the golden-test corpus. This module provides
only the registry structure and a fail-closed lookup.

The load-bearing distinction
----------------------------
A ``DeclineClass`` is an **observation** -- what the PSP told us. A
``RootCauseClass`` (see ``reclaim.contracts.enums``) is an **inference**. The
entire product thesis rests on one observation whose root cause is genuinely
ambiguous: Stripe's ``transaction_not_approved`` on India recurring rails means
"customer paused permissions to auto-debit, **or** didn't authenticate" (§2).
Those two causes demand opposite actions -- a retention path with human approval
versus a one-tap re-auth nudge -- so the taxonomy must preserve the ambiguity
rather than resolve it. ``PAYER_AUTHORIZATION_MISSING_AMBIGUOUS`` exists for
exactly that, and is flagged ``is_ambiguous=True`` so the triage gate routes it to
the diagnostician instead of the fast path.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field

from reclaim.contracts.enums import PspId, RootCauseClass
from reclaim.contracts.versions import DECLINE_TAXONOMY_VERSION

try:  # pragma: no cover
    from enum import StrEnum
except ImportError:  # pragma: no cover
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """Minimal StrEnum shim."""


__all__ = [
    "DECLINE_CLASS_META",
    "DeclineClass",
    "DeclineClassMeta",
    "DeclineCodeMapping",
    "MappingSource",
    "Retryability",
    "SEED_DECLINE_CODE_MAPPINGS",
    "TAXONOMY_VERSION",
    "lookup_canonical_class",
    "meta_for",
]

TAXONOMY_VERSION = DECLINE_TAXONOMY_VERSION


class Retryability(StrEnum):
    """What must change before another debit attempt has non-zero probability.

    This is the field that makes "a retry engine is the wrong tool" concrete: four
    of these eight members mean *no amount of retry timing helps*.
    """

    RETRY_SOFT = "retry_soft"
    """Same instrument, same mandate; a later time may succeed. Timing matters."""

    RETRY_AFTER_INCIDENT = "retry_after_incident"
    """Our-side or PSP-side technical failure. Retry only once the incident is
    resolved, and **suppress customer contact** meanwhile (§9.2 H6)."""

    REQUIRES_CUSTOMER_AUTHENTICATION = "requires_customer_authentication"
    """AFA / 3DS step-up must be completed by a human before any debit."""

    REQUIRES_NEW_CREDENTIAL = "requires_new_credential"
    """Card expired/replaced/token dead. Stripe: retries "only execute if you
    obtain a new payment method"."""

    REQUIRES_NEW_MANDATE = "requires_new_mandate"
    """Mandate is dead and immutable. Recovery is a re-registration *journey*; a
    retry is 0% and still costs a failed-attempt fee (§9.2 H3)."""

    NO_RETRY_TERMINAL = "no_retry_terminal"
    """Never retry: account closed, card reported lost/stolen, permanent block."""

    NO_RETRY_COMPLIANCE_HOLD = "no_retry_compliance_hold"
    """A hold forbids the attempt regardless of technical retryability."""

    UNKNOWN_FAIL_CLOSED = "unknown_fail_closed"
    """Unmapped code. Fail closed: no automated debit, escalate (§16 Data)."""

    @property
    def permits_automated_debit(self) -> bool:
        return self in (Retryability.RETRY_SOFT,)

    @property
    def needs_customer_journey(self) -> bool:
        return self in (
            Retryability.REQUIRES_CUSTOMER_AUTHENTICATION,
            Retryability.REQUIRES_NEW_CREDENTIAL,
            Retryability.REQUIRES_NEW_MANDATE,
        )


class DeclineClass(StrEnum):
    """Canonical decline taxonomy. One member per *distinguishable observation*.

    Members are intentionally finer-grained than the seven-way failure mix in
    §11.1, because the failure mix is a *generator target* while this is the
    vocabulary the policy engine must reason over.
    """

    # --- liquidity -----------------------------------------------------------
    INSUFFICIENT_FUNDS = "insufficient_funds"

    # --- issuer / transient --------------------------------------------------
    ISSUER_TRANSIENT_DECLINE = "issuer_transient_decline"
    ISSUER_UNAVAILABLE = "issuer_unavailable"

    # --- authentication / authorisation --------------------------------------
    AUTHENTICATION_REQUIRED = "authentication_required"
    """Stripe classifies this as a **hard decline**: retries "only execute if you
    obtain a new payment method" (§2). Above the AFA threshold RBI requires
    authentication on *every* debit, so this is recoverable via a journey."""

    PAYER_AUTHORIZATION_MISSING_AMBIGUOUS = "payer_authorization_missing_ambiguous"
    """Stripe ``transaction_not_approved`` on India recurring rails. Genuinely
    ambiguous between "payer paused auto-debit permissions" (churn intent or
    deliberate pause) and "payer never completed authentication" (missed
    notification). **Must not be collapsed.**"""

    # --- mandate lifecycle ---------------------------------------------------
    MANDATE_INVALID = "mandate_invalid"                    # payment_intent_mandate_invalid
    MANDATE_CANCELLED = "mandate_cancelled"                # india_recurring_payment_mandate_canceled
    MANDATE_PAUSED = "mandate_paused"
    MANDATE_EXPIRED = "mandate_expired"
    MANDATE_CAP_EXCEEDED = "mandate_cap_exceeded"
    PRE_DEBIT_NOTIFICATION_UNDELIVERED = "pre_debit_notification_undelivered"
    """Debit rejected because the mandatory >=24h notification never reached the
    payer. Our-side-adjacent: the fix is to re-notify, not to re-charge."""

    # --- credential lifecycle ------------------------------------------------
    CARD_EXPIRED = "card_expired"
    CARD_REPLACED_OR_TOKEN_INVALID = "card_replaced_or_token_invalid"
    CARD_LOST_OR_STOLEN = "card_lost_or_stolen"
    ACCOUNT_CLOSED_OR_INVALID = "account_closed_or_invalid"

    # --- risk ----------------------------------------------------------------
    RISK_BLOCKED_BY_ISSUER = "risk_blocked_by_issuer"
    RISK_BLOCKED_BY_PSP = "risk_blocked_by_psp"
    RISK_BLOCKED_BY_MERCHANT_RULE = "risk_blocked_by_merchant_rule"
    """Our own risk rule blocked it. Systemic when it hits a cohort (§9.2 H6)."""

    # --- our side / technical ------------------------------------------------
    PROCESSING_ERROR = "processing_error"
    ROUTING_OR_CONFIG_ERROR = "routing_or_config_error"
    RAIL_NOT_SUPPORTED = "rail_not_supported"

    # --- integrity -----------------------------------------------------------
    DUPLICATE_ATTEMPT_BLOCKED = "duplicate_attempt_blocked"
    NETWORK_RETRY_LIMIT_EXCEEDED = "network_retry_limit_exceeded"

    # --- fail-closed sentinel ------------------------------------------------
    UNKNOWN_UNMAPPED = "unknown_unmapped"
    """No mapping exists for the raw code. Never treated as retryable. Routes to
    the diagnostician (novel failure mode) and to a human if it stays unresolved."""


class DeclineClassMeta(BaseModel):
    """Per-class metadata. Read by the triage gate, policy engine and value model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decline_class: DeclineClass
    retryability: Retryability
    is_our_side: bool = Field(
        description="True when the failure is attributable to us (gateway, route, "
        "risk rule, undelivered notification). Drives contact suppression."
    )
    is_ambiguous: bool = Field(
        description="True when a single observation maps to root causes demanding "
        "opposite actions. Ambiguity forces the LLM path in the triage gate."
    )
    requires_new_authorization: bool
    counts_toward_network_retry_limit: bool
    candidate_root_causes: tuple[RootCauseClass, ...]
    notes: str = ""

    @property
    def eligible_for_fast_path(self) -> bool:
        """Deterministic zero-LLM fast path requires an unambiguous class with a
        single plausible root cause (§4 step 3). Value gating is applied
        separately by the triage gate."""
        return not self.is_ambiguous and len(self.candidate_root_causes) == 1


def _m(
    decline_class: DeclineClass,
    retryability: Retryability,
    *,
    our_side: bool = False,
    ambiguous: bool = False,
    new_auth: bool = False,
    counts_retry: bool = True,
    causes: tuple[RootCauseClass, ...],
    notes: str = "",
) -> DeclineClassMeta:
    return DeclineClassMeta(
        decline_class=decline_class,
        retryability=retryability,
        is_our_side=our_side,
        is_ambiguous=ambiguous,
        requires_new_authorization=new_auth,
        counts_toward_network_retry_limit=counts_retry,
        candidate_root_causes=causes,
        notes=notes,
    )


_H = RootCauseClass

DECLINE_CLASS_META: Mapping[DeclineClass, DeclineClassMeta] = {
    DeclineClass.INSUFFICIENT_FUNDS: _m(
        DeclineClass.INSUFFICIENT_FUNDS,
        Retryability.RETRY_SOFT,
        causes=(_H.H1_TIMING_LIQUIDITY,),
        notes="Timing against the payer's salary calendar is the whole intervention; "
        "no contact is usually the correct action.",
    ),
    DeclineClass.ISSUER_TRANSIENT_DECLINE: _m(
        DeclineClass.ISSUER_TRANSIENT_DECLINE,
        Retryability.RETRY_SOFT,
        causes=(_H.H1_TIMING_LIQUIDITY,),
    ),
    DeclineClass.ISSUER_UNAVAILABLE: _m(
        DeclineClass.ISSUER_UNAVAILABLE,
        Retryability.RETRY_AFTER_INCIDENT,
        counts_retry=False,
        causes=(_H.H6_OUR_SIDE_SYSTEMIC,),
        notes="Not our fault, but cohort-correlated; suppress contact while open.",
    ),
    DeclineClass.AUTHENTICATION_REQUIRED: _m(
        DeclineClass.AUTHENTICATION_REQUIRED,
        Retryability.REQUIRES_CUSTOMER_AUTHENTICATION,
        new_auth=True,
        causes=(_H.H4_AFA_STEP_UP_INCOMPLETE,),
        notes="Stripe hard decline. Above the AFA threshold RBI requires AFA on "
        "every debit, so a bare retry is 0%.",
    ),
    DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS: _m(
        DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS,
        Retryability.REQUIRES_CUSTOMER_AUTHENTICATION,
        ambiguous=True,
        new_auth=True,
        causes=(
            _H.H4_AFA_STEP_UP_INCOMPLETE,
            _H.H3_MANDATE_DEAD_OR_PAUSED,
            _H.H5_DELIBERATE_CHURN_INTENT,
        ),
        notes="Stripe transaction_not_approved. The highest-value inference in the "
        "system: deliberate pause and missed notification look identical in the "
        "data but require opposite actions.",
    ),
    DeclineClass.MANDATE_INVALID: _m(
        DeclineClass.MANDATE_INVALID,
        Retryability.REQUIRES_NEW_MANDATE,
        new_auth=True,
        counts_retry=False,
        causes=(_H.H3_MANDATE_DEAD_OR_PAUSED,),
    ),
    DeclineClass.MANDATE_CANCELLED: _m(
        DeclineClass.MANDATE_CANCELLED,
        Retryability.REQUIRES_NEW_MANDATE,
        new_auth=True,
        counts_retry=False,
        causes=(_H.H3_MANDATE_DEAD_OR_PAUSED,),
        notes="Mandates cannot be cancelled or updated via API, so recovery is a "
        "new registration journey.",
    ),
    DeclineClass.MANDATE_PAUSED: _m(
        DeclineClass.MANDATE_PAUSED,
        Retryability.REQUIRES_NEW_MANDATE,
        ambiguous=True,
        new_auth=True,
        counts_retry=False,
        causes=(_H.H3_MANDATE_DEAD_OR_PAUSED, _H.H5_DELIBERATE_CHURN_INTENT),
        notes="A payer-initiated pause may signal churn intent; a payment nudge "
        "would be the wrong action.",
    ),
    DeclineClass.MANDATE_EXPIRED: _m(
        DeclineClass.MANDATE_EXPIRED,
        Retryability.REQUIRES_NEW_MANDATE,
        new_auth=True,
        counts_retry=False,
        causes=(_H.H3_MANDATE_DEAD_OR_PAUSED,),
    ),
    DeclineClass.MANDATE_CAP_EXCEEDED: _m(
        DeclineClass.MANDATE_CAP_EXCEEDED,
        Retryability.REQUIRES_NEW_MANDATE,
        our_side=True,
        new_auth=True,
        counts_retry=False,
        causes=(_H.H6_OUR_SIDE_SYSTEMIC, _H.H3_MANDATE_DEAD_OR_PAUSED),
        notes="We asked for more than the mandate permits. Our defect; a new "
        "mandate with a higher cap is the fix, not a retry.",
    ),
    DeclineClass.PRE_DEBIT_NOTIFICATION_UNDELIVERED: _m(
        DeclineClass.PRE_DEBIT_NOTIFICATION_UNDELIVERED,
        Retryability.RETRY_AFTER_INCIDENT,
        our_side=True,
        counts_retry=False,
        causes=(_H.H6_OUR_SIDE_SYSTEMIC, _H.H4_AFA_STEP_UP_INCOMPLETE),
        notes="Re-notify and re-schedule respecting the >=24h window. Never "
        "re-charge without a satisfied window (invariant #4).",
    ),
    DeclineClass.CARD_EXPIRED: _m(
        DeclineClass.CARD_EXPIRED,
        Retryability.REQUIRES_NEW_CREDENTIAL,
        new_auth=True,
        counts_retry=False,
        causes=(_H.H2_CREDENTIAL_LIFECYCLE,),
    ),
    DeclineClass.CARD_REPLACED_OR_TOKEN_INVALID: _m(
        DeclineClass.CARD_REPLACED_OR_TOKEN_INVALID,
        Retryability.REQUIRES_NEW_CREDENTIAL,
        new_auth=True,
        counts_retry=False,
        causes=(_H.H2_CREDENTIAL_LIFECYCLE,),
    ),
    DeclineClass.CARD_LOST_OR_STOLEN: _m(
        DeclineClass.CARD_LOST_OR_STOLEN,
        Retryability.NO_RETRY_TERMINAL,
        counts_retry=False,
        causes=(_H.H2_CREDENTIAL_LIFECYCLE,),
        notes="Never retry. A credential-update journey is permitted; a debit is not.",
    ),
    DeclineClass.ACCOUNT_CLOSED_OR_INVALID: _m(
        DeclineClass.ACCOUNT_CLOSED_OR_INVALID,
        Retryability.NO_RETRY_TERMINAL,
        counts_retry=False,
        causes=(_H.H2_CREDENTIAL_LIFECYCLE,),
    ),
    DeclineClass.RISK_BLOCKED_BY_ISSUER: _m(
        DeclineClass.RISK_BLOCKED_BY_ISSUER,
        Retryability.NO_RETRY_TERMINAL,
        counts_retry=False,
        causes=(_H.H6_OUR_SIDE_SYSTEMIC, _H.H7_COMMERCIAL_DISPUTE),
        notes="Repeated attempts against an issuer block risk network penalties.",
    ),
    DeclineClass.RISK_BLOCKED_BY_PSP: _m(
        DeclineClass.RISK_BLOCKED_BY_PSP,
        Retryability.RETRY_AFTER_INCIDENT,
        counts_retry=False,
        causes=(_H.H6_OUR_SIDE_SYSTEMIC,),
    ),
    DeclineClass.RISK_BLOCKED_BY_MERCHANT_RULE: _m(
        DeclineClass.RISK_BLOCKED_BY_MERCHANT_RULE,
        Retryability.RETRY_AFTER_INCIDENT,
        our_side=True,
        counts_retry=False,
        causes=(_H.H6_OUR_SIDE_SYSTEMIC,),
        notes="Our own rule. Contacting the customer would be a false action.",
    ),
    DeclineClass.PROCESSING_ERROR: _m(
        DeclineClass.PROCESSING_ERROR,
        Retryability.RETRY_AFTER_INCIDENT,
        our_side=True,
        counts_retry=False,
        causes=(_H.H6_OUR_SIDE_SYSTEMIC,),
        notes="The demo's systemic-suppression beat: 60 of these in 12 minutes on "
        "one issuer x BIN is an incident, not 60 customer problems.",
    ),
    DeclineClass.ROUTING_OR_CONFIG_ERROR: _m(
        DeclineClass.ROUTING_OR_CONFIG_ERROR,
        Retryability.RETRY_AFTER_INCIDENT,
        our_side=True,
        counts_retry=False,
        causes=(_H.H6_OUR_SIDE_SYSTEMIC,),
    ),
    DeclineClass.RAIL_NOT_SUPPORTED: _m(
        DeclineClass.RAIL_NOT_SUPPORTED,
        Retryability.NO_RETRY_TERMINAL,
        our_side=True,
        counts_retry=False,
        causes=(_H.H6_OUR_SIDE_SYSTEMIC,),
    ),
    DeclineClass.DUPLICATE_ATTEMPT_BLOCKED: _m(
        DeclineClass.DUPLICATE_ATTEMPT_BLOCKED,
        Retryability.NO_RETRY_TERMINAL,
        our_side=True,
        counts_retry=False,
        causes=(_H.H6_OUR_SIDE_SYSTEMIC,),
        notes="Idempotency working as intended. Reconcile; do not re-attempt.",
    ),
    DeclineClass.NETWORK_RETRY_LIMIT_EXCEEDED: _m(
        DeclineClass.NETWORK_RETRY_LIMIT_EXCEEDED,
        Retryability.NO_RETRY_COMPLIANCE_HOLD,
        counts_retry=False,
        causes=(_H.H1_TIMING_LIQUIDITY,),
        notes="Card-network excessive-retry limit reached. Zero further debits.",
    ),
    DeclineClass.UNKNOWN_UNMAPPED: _m(
        DeclineClass.UNKNOWN_UNMAPPED,
        Retryability.UNKNOWN_FAIL_CLOSED,
        ambiguous=True,
        counts_retry=False,
        causes=(_H.UNKNOWN,),
        notes="Novel failure mode. Justifies the agent (no labels exist) and "
        "forbids automated money movement.",
    ),
}

# Exhaustiveness is a contract, not a hope.
_missing = set(DeclineClass) - set(DECLINE_CLASS_META)
if _missing:  # pragma: no cover - import-time guard
    raise RuntimeError(f"DECLINE_CLASS_META is missing entries for: {sorted(_missing)}")
del _missing


def meta_for(decline_class: DeclineClass) -> DeclineClassMeta:
    return DECLINE_CLASS_META[decline_class]


# ---------------------------------------------------------------------------
# PSP code -> canonical class mapping (format + seeds only)
# ---------------------------------------------------------------------------


class MappingSource(StrEnum):
    """Provenance of a mapping row. ``LLM_PROPOSED`` rows are **never** used at
    runtime until a human merges them (§5: "LLM only *proposes* mappings for
    unseen codes, offline, human-merged")."""

    PSP_DOCUMENTATION = "psp_documentation"
    OBSERVED_AND_HUMAN_LABELLED = "observed_and_human_labelled"
    LLM_PROPOSED = "llm_proposed"


class DeclineCodeMapping(BaseModel):
    """One row of the versioned lookup table (§5, §7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    psp: PspId
    raw_code: str = Field(min_length=1, max_length=128)
    canonical_class: DeclineClass
    taxonomy_version: str = TAXONOMY_VERSION
    source: MappingSource
    raw_message_pattern: str | None = Field(
        default=None,
        description="Optional regex disambiguator for PSPs that overload one code. "
        "Phase 1 applies it only when raw_code alone is insufficient.",
    )
    merged_by: str | None = Field(
        default=None, description="Required for LLM_PROPOSED rows before activation."
    )
    citation: str = Field(
        default="",
        description="Where this mapping came from. Golden tests assert non-empty "
        "for PSP_DOCUMENTATION rows.",
    )

    @property
    def is_active(self) -> bool:
        """LLM-proposed mappings are inert until a human merges them."""
        return self.source is not MappingSource.LLM_PROPOSED or self.merged_by is not None


#: Seed rows. Only codes HACKATHON_PLAN.md cites verbatim (§11.2, §26) are frozen
#: here, so that golden tests exist from hour zero. The full table is Phase 1.
SEED_DECLINE_CODE_MAPPINGS: tuple[DeclineCodeMapping, ...] = (
    DeclineCodeMapping(
        psp=PspId.STRIPE_TEST,
        raw_code="authentication_required",
        canonical_class=DeclineClass.AUTHENTICATION_REQUIRED,
        source=MappingSource.PSP_DOCUMENTATION,
        citation="Stripe Docs, Automate payment retries -- hard-decline code list.",
    ),
    DeclineCodeMapping(
        psp=PspId.STRIPE_TEST,
        raw_code="transaction_not_approved",
        canonical_class=DeclineClass.PAYER_AUTHORIZATION_MISSING_AMBIGUOUS,
        source=MappingSource.PSP_DOCUMENTATION,
        citation="Stripe Docs, India recurring payments -- 'customer paused "
        "permissions to auto-debit, or didn't authenticate'.",
    ),
    DeclineCodeMapping(
        psp=PspId.STRIPE_TEST,
        raw_code="payment_intent_mandate_invalid",
        canonical_class=DeclineClass.MANDATE_INVALID,
        source=MappingSource.PSP_DOCUMENTATION,
        citation="Stripe Docs, India recurring payments.",
    ),
    DeclineCodeMapping(
        psp=PspId.STRIPE_TEST,
        raw_code="india_recurring_payment_mandate_canceled",
        canonical_class=DeclineClass.MANDATE_CANCELLED,
        source=MappingSource.PSP_DOCUMENTATION,
        citation="Stripe Docs, India recurring payments.",
    ),
    DeclineCodeMapping(
        psp=PspId.STRIPE_TEST,
        raw_code="processing_error",
        canonical_class=DeclineClass.PROCESSING_ERROR,
        source=MappingSource.PSP_DOCUMENTATION,
        citation="Stripe Docs, decline codes.",
    ),
    DeclineCodeMapping(
        psp=PspId.STRIPE_TEST,
        raw_code="insufficient_funds",
        canonical_class=DeclineClass.INSUFFICIENT_FUNDS,
        source=MappingSource.PSP_DOCUMENTATION,
        citation="Stripe Docs, decline codes.",
    ),
    DeclineCodeMapping(
        psp=PspId.STRIPE_TEST,
        raw_code="expired_card",
        canonical_class=DeclineClass.CARD_EXPIRED,
        source=MappingSource.PSP_DOCUMENTATION,
        citation="Stripe Docs, decline codes.",
    ),
)


def lookup_canonical_class(
    psp: PspId,
    raw_code: str,
    table: tuple[DeclineCodeMapping, ...] = SEED_DECLINE_CODE_MAPPINGS,
) -> DeclineClass:
    """Registry lookup. Fail closed on anything unmapped.

    This is *not* the normaliser -- message-pattern disambiguation, per-PSP
    quirks and the golden-test corpus are Phase 1 (§18.1 item 4). It exists in
    Phase 0 so the fail-closed behaviour is a frozen contract rather than a
    Phase-1 implementation choice.
    """
    needle = raw_code.strip().lower()
    for row in table:
        if row.psp is psp and row.raw_code.lower() == needle and row.is_active:
            return row.canonical_class
    return DeclineClass.UNKNOWN_UNMAPPED
