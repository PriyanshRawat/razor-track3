"""Assembling the closed fact vocabulary from state the model cannot influence.

``PolicyFactKey`` is a closed vocabulary precisely so §14.4's fourth defence is
enforced by a type rather than by reviewer discipline. This module is the other
half of that: every fact below is derived from the ledger, the obligation, the
consent record, the outbox or the clock -- never from anything a planner or a
model proposed. The proposal selects *which* rules run; it never supplies what
they see. The single exception the contract allows, ``DIAGNOSIS_CONFIDENCE``, is
not built here at all, because this rule set does not gate on it (see
``rules.NOT_YET_ENCODED``).

No I/O. ``FactContext`` is a plain record the caller fills from the database, so
this module can be tested without one and cannot reach past what it was handed.
It *does* use the clock -- that is the whole point of living outside
``reclaim.contracts``, where §12.5.4 forbids it.

Quiet hours (JC-43, and the cost it named)
------------------------------------------
``resolve_quiet_hours`` decides *which* window governs and says plainly that the
arithmetic is still the engine's to get wrong. ``is_within_contact_window`` is
where that arithmetic lives, and it is the only place in this package that
converts an instant into a local hour. It reads the zone off the window it was
handed -- never ``datetime.now()``'s zone, never the server's -- so invariant #3
does not move when the deploy region does.

Fail-closed defaults, and where they are *not* used
---------------------------------------------------
An absent consent profile answers ``CONSENT_RECORD_EXISTS = False``, which the
consent gate treats as a denial (§14.1: "absent consent record => no contact").
A mandate-backed rail with no mandate record answers
``MANDATE_IS_DEBITABLE = False`` (§10.1: "unknown => assume invalid").

Two facts are deliberately *not* fail-closed and must be read as what they are:
``IS_DECLARED_HOLIDAY`` is False unless the caller supplies a calendar, and
``CONTAINS_BANNED_PHRASE`` is False while every slot value is a formatted number.
Both are recorded in ``rules.NOT_YET_ENCODED``: the rule is real, the fact is not
yet sourced, and a fact that is always False is a gate that has never fired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Final, FrozenSet, Mapping
from zoneinfo import ZoneInfo

from reclaim.contracts.actions import ActionEnvelope, ActionType, SendMessage
from reclaim.contracts.case import RiskCase
from reclaim.contracts.decline_taxonomy import DECLINE_CLASS_META, Retryability
from reclaim.contracts.enums import (
    Channel,
    MessageIntent,
    ObligationStatus,
    PolicyCategory,
    StopReason,
)
from reclaim.contracts.money import Money
from reclaim.contracts.obligations import (
    ConsentProfile,
    Hold,
    Mandate,
    Obligation,
    QuietHours,
    has_consent,
)
from reclaim.contracts.policy_format import (
    ENUM_FACT_VOCABULARIES,
    FACT_TYPES,
    FactType,
    PolicyFactKey as F,
    PolicyThresholds,
    resolve_quiet_hours,
)
from reclaim.contracts.rails import rail_spec
from reclaim.policy.templates import TEMPLATE_REGISTRY, contains_banned_phrase

__all__ = [
    "DPDP_PURPOSE_BY_INTENT",
    "FactContext",
    "SOFT_RETRYABILITIES",
    "build_facts",
    "contact_window_end",
    "is_within_contact_window",
    "validate_facts",
]


#: The DPDP purpose each message intent must have been consented for. A single
#: "we have consent" bit would collapse purpose limitation entirely: consent
#: captured to send a service apology does not cover a payment reminder. Kept as
#: a table so the mapping is reviewable, and total over ``MessageIntent`` (guarded
#: at import) so a new intent cannot default into an existing purpose.
DPDP_PURPOSE_BY_INTENT: Mapping[MessageIntent, str] = {
    MessageIntent.PRE_DEBIT_NOTIFICATION: "payment_recovery",
    MessageIntent.PAYMENT_FAILED_INFORM: "payment_recovery",
    MessageIntent.CREDENTIAL_UPDATE_REQUEST: "payment_recovery",
    MessageIntent.MANDATE_REAUTH_REQUEST: "payment_recovery",
    MessageIntent.AFA_COMPLETION_REQUEST: "payment_recovery",
    MessageIntent.INVOICE_CORRECTION: "account_servicing",
    MessageIntent.PAYMENT_REMINDER: "payment_recovery",
    MessageIntent.PROMISE_FOLLOW_UP: "payment_recovery",
    MessageIntent.RETENTION_OUTREACH: "retention",
    MessageIntent.SERVICE_APOLOGY: "account_servicing",
}

_missing_intents = set(MessageIntent) - set(DPDP_PURPOSE_BY_INTENT)
if _missing_intents:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "DPDP_PURPOSE_BY_INTENT is missing: "
        + ", ".join(sorted(i.value for i in _missing_intents))
    )
del _missing_intents


#: The only retryability under which another debit attempt has a chance without
#: something changing first. Everything else is "hard" for the purposes of
#: §14.3's "hard decline => zero further debits" -- including
#: ``RETRY_AFTER_INCIDENT``, where a retry before the incident resolves has the
#: same zero probability and a *different* correct action (open/await the
#: incident, suppress contact). Derived from the frozen taxonomy rather than
#: listed as decline classes, so a new class inherits the right answer.
SOFT_RETRYABILITIES: Final[frozenset[Retryability]] = frozenset(
    {Retryability.RETRY_SOFT}
)

#: The §14.1 hard stops that have a fact of their own. Bereavement is
#: deliberately absent -- it has no dedicated fact, and ``HAS_ACTIVE_HOLD``
#: (any open hold at all) is what keeps it visible to the engine.
_HOLD_FACT_BY_KIND: Mapping[StopReason, F] = {
    StopReason.HARD_STOP_DISPUTE: F.HAS_OPEN_DISPUTE,
    StopReason.HARD_STOP_HARDSHIP: F.HAS_HARDSHIP_FLAG,
    StopReason.HARD_STOP_LEGAL_HOLD: F.HAS_LEGAL_HOLD,
    StopReason.HARD_STOP_CHARGEBACK: F.HAS_OPEN_CHARGEBACK,
    StopReason.HARD_STOP_OPT_OUT: F.IS_OPTED_OUT,
}

#: Obligation statuses under which money is still owed. Anything else means the
#: obligation is settled, credited, written off or void -- and §14.3's
#: reconciliation rule says stop.
_OUTSTANDING_STATUSES: Final[frozenset[ObligationStatus]] = frozenset(
    {ObligationStatus.OPEN, ObligationStatus.PARTIALLY_PAID}
)


@dataclass(frozen=True)
class FactContext:
    """Everything the fact builder is allowed to read, already read for it.

    A plain record rather than a database handle: this module does no I/O, so a
    fact cannot come from anywhere the caller did not put here, and the builder
    is testable without a connection. The counts are supplied rather than derived
    because they are queries -- ``flow.py`` runs them against the outbox.
    """

    case: RiskCase
    obligation: Obligation
    now: datetime
    thresholds: PolicyThresholds
    consent: ConsentProfile | None = None
    mandate: Mandate | None = None
    holds: tuple[Hold, ...] = ()
    debit_attempts_this_window: int = 0
    contacts_on_channel_last_7d: int = 0
    contacts_total_this_case: int = 0
    used_idempotency_keys: FrozenSet[str] = frozenset()
    declared_holidays: FrozenSet[date] = frozenset()


# ---------------------------------------------------------------------------
# Quiet hours -- the one place an instant becomes a local hour
# ---------------------------------------------------------------------------


def _local(instant: datetime, window: QuietHours) -> datetime:
    return instant.astimezone(ZoneInfo(window.timezone_name))


def is_within_contact_window(instant: datetime, window: QuietHours) -> bool:
    """Whether ``instant`` falls inside ``window``, in the window's own zone.

    ``QuietHours`` speaks in whole local hours and forbids an inverted window, so
    this is a half-open ``[start, end)`` comparison on the local hour and nothing
    more. ``end_hour_local`` may be 24, which reads as "until local midnight" and
    needs no special case.
    """
    return window.start_hour_local <= _local(instant, window).hour < window.end_hour_local


def contact_window_end(instant: datetime, window: QuietHours) -> datetime:
    """The next instant at which ``window`` closes, in UTC.

    Built by localising a naive wall-clock datetime rather than by adding a
    ``timedelta`` to an aware one: across a DST transition those differ, and the
    answer a DEFER waits on must be a wall-clock 19:00 in the payer's zone, not
    "ten hours after midnight UTC-shifted".
    """
    zone = ZoneInfo(window.timezone_name)
    local = instant.astimezone(zone)
    midnight = local.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    close = (midnight + timedelta(hours=window.end_hour_local)).replace(tzinfo=zone)
    if close <= instant:
        close = (
            midnight + timedelta(days=1, hours=window.end_hour_local)
        ).replace(tzinfo=zone)
    return close.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Fact assembly
# ---------------------------------------------------------------------------


def _open_holds(context: FactContext) -> tuple[Hold, ...]:
    return tuple(
        hold
        for hold in context.holds
        if hold.released_at is None and hold.opened_at <= context.now
    )


def _hold_facts(context: FactContext) -> dict[F, Any]:
    open_holds = _open_holds(context)
    kinds = {hold.kind for hold in open_holds}
    facts: dict[F, Any] = {
        fact: kind in kinds for kind, fact in _HOLD_FACT_BY_KIND.items()
    }
    facts[F.HAS_ACTIVE_HOLD] = bool(open_holds)
    facts[F.IN_SUPPRESSED_COHORT] = context.case.cohort_id is not None
    facts[F.HAS_OPEN_INCIDENT_ATTRIBUTABLE_TO_US] = context.case.incident_id is not None
    return facts


def _opted_out(context: FactContext, from_holds: bool) -> bool:
    """Opt-out is either a hard-stop hold or a consent record that was withdrawn.

    A withdrawn record is not the same as an absent one: both block contact, but
    only the former is an opt-out we must never walk back (invariant #2). The
    absent case is handled by ``CONSENT_RECORD_EXISTS``.
    """
    if from_holds:
        return True
    profile = context.consent
    if profile is None:
        return False
    return any(not record.is_effective for record in profile.records)


def _integrity_facts(envelope: ActionEnvelope, context: FactContext) -> dict[F, Any]:
    return {
        F.IDEMPOTENCY_KEY_ALREADY_USED: envelope.idempotency_key
        in context.used_idempotency_keys,
        F.OBLIGATION_ALREADY_SETTLED: context.obligation.status
        not in _OUTSTANDING_STATUSES,
    }


def _debit_facts(envelope: ActionEnvelope, context: FactContext) -> dict[F, Any]:
    action = envelope.action
    spec = rail_spec(action.rail)
    if not spec.is_mandate_backed:
        # Nothing to invalidate: a customer-present card payment carries its own
        # authorisation. Not the same as "we checked and it is fine".
        debitable = True
    else:
        mandate = context.mandate
        debitable = (
            mandate is not None
            and mandate.is_debitable
            and mandate.permits_amount(action.amount)
        )

    decline_class = context.case.canonical_decline_class
    last_was_hard = (
        decline_class is not None
        and DECLINE_CLASS_META[decline_class].retryability not in SOFT_RETRYABILITIES
    )

    return {
        F.DEBIT_AMOUNT: action.amount,
        F.RAIL: action.rail.value,
        F.MANDATE_IS_DEBITABLE: debitable,
        F.LAST_DECLINE_WAS_HARD: last_was_hard,
        F.NETWORK_RETRY_COUNT_THIS_WINDOW: context.debit_attempts_this_window,
    }


def _contact_facts(envelope: ActionEnvelope, context: FactContext) -> dict[F, Any]:
    action = envelope.action
    channel: Channel = action.channel
    profile = context.consent
    record = profile.record_for(channel) if profile is not None else None

    window = resolve_quiet_hours(profile, context.thresholds)
    local_date = _local(context.now, window).date()

    template = TEMPLATE_REGISTRY.get(getattr(action, "template_id", ""))
    slot_values = getattr(action, "slots", ())

    facts: dict[F, Any] = {
        F.IS_ON_DNC_LIST: profile.on_dnc_list if profile is not None else False,
        F.CONSENT_RECORD_EXISTS: record is not None,
        F.HAS_CHANNEL_CONSENT: has_consent(profile, channel),
        F.CONSENT_LANGUAGE_MATCHES: profile is not None
        and profile.language is action.language,
        F.IS_WITHIN_QUIET_HOURS: is_within_contact_window(context.now, window),
        F.QUIET_HOURS_END_AT: contact_window_end(context.now, window),
        F.IS_DECLARED_HOLIDAY: local_date in context.declared_holidays,
        F.CONTACTS_ON_CHANNEL_LAST_7D: context.contacts_on_channel_last_7d,
        F.CONTACTS_TOTAL_THIS_CASE: context.contacts_total_this_case,
        F.TEMPLATE_IS_DLT_REGISTERED: template is not None and template.dlt_registered,
        F.TEMPLATE_LANGUAGE: action.language.value,
        F.HAS_FREE_TEXT_SLOT: any(slot.free_text for slot in slot_values),
        F.CONTAINS_BANNED_PHRASE: any(
            contains_banned_phrase(slot.value) for slot in slot_values
        ),
    }

    if isinstance(action, SendMessage):
        required_purpose = DPDP_PURPOSE_BY_INTENT[action.intent]
        facts[F.DPDP_PURPOSE_COVERS_ACTION] = (
            record is not None and record.dpdp_purpose == required_purpose
        )
    return facts


def build_facts(
    envelope: ActionEnvelope, context: FactContext
) -> dict[F, Any]:
    """The fact bundle for one proposed action.

    Action-specific by design: ``debit_amount`` is not a fact about a message and
    ``template_is_dlt_registered`` is not a fact about a debit. The engine turns a
    fact it was not given into an unevaluable rule and, usually, a fail-closed
    DENY -- which is why proposing a verb this module does not build facts for is
    refused rather than waved through (``test_policy_rules.py`` pins that).
    """
    spec_categories = _categories_for(envelope)
    facts: dict[F, Any] = {}
    facts.update(_hold_facts(context))
    facts[F.IS_OPTED_OUT] = _opted_out(context, facts[F.IS_OPTED_OUT])
    facts.update(_integrity_facts(envelope, context))
    facts[F.SEGMENT] = context.case.segment.value

    if envelope.action.action_type is ActionType.SCHEDULE_DEBIT:
        facts.update(_debit_facts(envelope, context))
    if PolicyCategory.CONSENT_AND_CHANNEL in spec_categories:
        facts.update(_contact_facts(envelope, context))
    return facts


def _categories_for(envelope: ActionEnvelope) -> frozenset[PolicyCategory]:
    return envelope.spec.policy_categories


# ---------------------------------------------------------------------------
# Type validation
# ---------------------------------------------------------------------------


def _type_error(fact: F, expected: str, value: Any) -> ValueError:
    return ValueError(
        f"policy fact {fact.value!r} is declared {FACT_TYPES[fact].value} and must "
        f"be {expected}; got {type(value).__name__} ({value!r}). A fact of the "
        "wrong type compares silently and wrongly -- the predicate reads correctly "
        "and evaluates to nonsense (CONTRACTS.md §6, defect 2)."
    )


def validate_facts(facts: Mapping[F, Any]) -> None:
    """Check every supplied fact against its frozen ``FactType``.

    ``FactPredicate`` already checks the *literal* side of a comparison. This is
    the other side: nothing checks that the bundle put a ``Money`` where the fact
    is declared MONEY, and a ``Rail`` member where a ``str`` is expected compares
    equal to its own value anyway -- so the bug that survives is the one where the
    types are merely *similar*.
    """
    for fact, value in facts.items():
        declared = FACT_TYPES[fact]
        if declared is FactType.BOOLEAN:
            if not isinstance(value, bool):
                raise _type_error(fact, "a bool", value)
        elif declared is FactType.MONEY:
            if not isinstance(value, Money):
                raise _type_error(fact, "a Money", value)
        elif declared in (FactType.COUNT, FactType.DURATION_HOURS):
            if isinstance(value, bool) or not isinstance(value, int):
                raise _type_error(fact, "an int", value)
        elif declared is FactType.TIMESTAMP:
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise _type_error(fact, "a timezone-aware datetime", value)
        elif declared is FactType.PROBABILITY:
            if not isinstance(value, Decimal):
                raise _type_error(fact, "a Decimal in [0, 1]", value)
        elif declared is FactType.ENUM:
            vocabulary = ENUM_FACT_VOCABULARIES[fact]
            legal = {str(member.value) for member in vocabulary}
            if not isinstance(value, str) or value not in legal:
                raise _type_error(
                    fact, f"one of {sorted(legal)} as a str", value
                )
        else:  # pragma: no cover - FactType is closed and fully handled above
            raise ValueError(f"unhandled fact type {declared!r}")
