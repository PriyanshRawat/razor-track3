"""The policy rule format -- structure only.

**There are no rules in this file, and that is deliberate.** Phase 0 freezes the
*shape* a rule may take; Phase 1 writes the rule set. Separating them means the
rule set can be reviewed, diffed and version-bumped by a compliance reader without
touching Python, and it means a rule cannot smuggle in behaviour: a rule is data,
evaluated by one engine, over a closed set of facts.

Four properties are load-bearing, each pinned by a test:

1. **Closed fact vocabulary.** A rule may only test a ``PolicyFactKey``. Facts are
   assembled by the engine from PSP state, consent records, the ledger and the
   clock -- never from model output. §14.4's fourth defence ("the policy engine
   re-validates every proposal against state the model cannot influence") is
   enforced by the type of the predicate, not by reviewer discipline.
2. **Rules ship with their own tests.** ``PolicyRule.test_cases`` must contain at
   least one matching and one non-matching case. §14.1 requires per-rule allow+deny
   tests; making them a *field* means an untested rule cannot be loaded at all,
   rather than merely failing a coverage report someone can waive.
3. **Fail closed.** ``combine_verdicts`` returns DENY when nothing evaluated, and
   the strictest effect wins regardless of ordering. There is no configuration that
   turns an unevaluated action into an allowed one.
4. **Every verdict is retained, including allows** (§14.1), because the Recovery
   Receipt shows "what policy allowed and what it denied, with rule IDs".

CONTRACT DECISION (JC-20) -- effects are a lattice, not a rule priority
-----------------------------------------------------------------------
The plan says "rule conflicts fail closed" but does not define a conflict
resolution order. Rather than give rules priorities (which invites a
priority-inversion bug where a low-priority DENY is overridden), effects form a
total order -- ``DENY > DEFER > ALLOW_WITH_APPROVAL > ALLOW`` -- and the strictest
verdict wins. Order-independence is a tested property. The cost: two rules cannot
express "this deny is overridden in situation X"; the override must be written into
the *condition* of the denying rule, where a reviewer can see it.

CONTRACT DECISION (JC-21) -- an advisory verdict never blocks
--------------------------------------------------------------
``RuleSeverity.ADVISORY`` exists so that soft checks (tone, style, a heuristic
smell) can be recorded on the receipt without acquiring veto power. Advisory
verdicts are excluded from the lattice and surface as ``advisory_rule_ids``. If a
check should block, it must be marked BLOCKING -- that is a deliberate, reviewable
decision rather than a side effect of how the check was written.

CONTRACT DECISION (JC-22) -- thresholds live here, rail floors do not
----------------------------------------------------------------------
``PolicyThresholds`` holds the numbers *we* choose (§11.2 explicitly makes the AFA
threshold config so an RBI change is a one-line edit). Rail mechanics live in
``reclaim.contracts.rails`` and configuration may only add caution on top of them.
``max_concession_value`` is present, is ``Money.zero()``, and is validated to stay
zero: invariant #7 should be visible in the config a reviewer reads, not merely
absent from it.

CONTRACT DECISION (JC-43) -- quiet hours have one owner and a named fallback zone
---------------------------------------------------------------------------------
§14.1 permits two sources for a contact window and orders neither: the payer's
own (``obligations.ConsentProfile.quiet_hours``, which carries an IANA zone) and
the configured global default (``PolicyThresholds.quiet_hours_*``, which carries
clock times and no zone at all). Two sources with no rule between them is not a
tie -- it is whichever one the next caller reaches for, and invariant #3 is "no
contact outside quiet hours, **in any timezone**".

``resolve_quiet_hours`` is the only place the question is answered. A stated
preference wins, evaluated in its own ``timezone_name``; otherwise the configured
window, read as ``FALLBACK_QUIET_HOURS_TIMEZONE``. That zone is Asia/Kolkata
because RECLAIM is India-first and §14.1's default window (09:00-19:00) is written
in IST. The server's zone was rejected as the fallback: it would put invariant #3
one deploy-region change away from false, with nothing in the config to show that
it had moved.

Three costs, all real. ``ConsentProfile.quiet_hours`` had to become optional
(CONTRACTS.md §7 N7) -- a default is indistinguishable from a preference, so while
one existed the configured window could never legitimately apply to anybody.
``PolicyThresholds`` now refuses a boundary that is not on the hour, because
``QuietHours`` speaks in whole local hours and a configured 09:30 would otherwise
be truncated to 09:00, widening the window we may contact in. And this function
resolves *which* window governs, not whether an instant falls inside it: the
conversion still happens in the engine, so one place to decide the window is not
the same as one place to do the arithmetic.
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Final, Literal, Mapping, Sequence, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from reclaim.contracts.canonical import digest
from reclaim.contracts.enums import (
    AutonomyTier,
    Channel,
    Language,
    PolicyCategory,
    PolicyEffect,
    Rail,
    RuleSeverity,
    Segment,
)
from reclaim.contracts.money import Money
from reclaim.contracts.obligations import ConsentProfile, QuietHours
from reclaim.contracts.temporal import UtcDatetime
from reclaim.contracts.units import fixed_point, probability
from reclaim.contracts.versions import POLICY_FORMAT_VERSION

__all__ = [
    "AllOf",
    "AnyOf",
    "ComparisonOperator",
    "DEFAULT_MAX_GRACE_PERIOD_DAYS_BY_SEGMENT",
    "ENUM_FACT_VOCABULARIES",
    "FALLBACK_QUIET_HOURS_TIMEZONE",
    "FactPredicate",
    "FactType",
    "MAX_PREDICATE_DEPTH",
    "Not",
    "POLICY_FORMAT_VERSION",
    "PolicyDecision",
    "PolicyFactKey",
    "PolicyRule",
    "PolicyRuleSet",
    "PolicyThresholds",
    "PolicyVerdict",
    "Predicate",
    "RuleTestCase",
    "combine_verdicts",
    "fact_type",
    "resolve_quiet_hours",
]

MAX_PREDICATE_DEPTH: Final[int] = 8


class FactType(str, Enum):
    """The value domain of a fact. Determines which operators are legal."""

    BOOLEAN = "boolean"
    MONEY = "money"
    COUNT = "count"
    DURATION_HOURS = "duration_hours"
    TIMESTAMP = "timestamp"
    ENUM = "enum"
    PROBABILITY = "probability"


class PolicyFactKey(str, Enum):
    """The closed vocabulary a rule may test.

    Every fact is derived by the engine from state the model cannot influence:
    PSP responses, consent records, the ledger, the case history, the clock. There
    is deliberately no fact whose value comes from model output -- diagnosis
    confidence is the single exception, and it can only ever *raise* the tier.
    """

    # consent & channel
    HAS_CHANNEL_CONSENT = "has_channel_consent"
    CONSENT_RECORD_EXISTS = "consent_record_exists"
    IS_ON_DNC_LIST = "is_on_dnc_list"
    IS_OPTED_OUT = "is_opted_out"
    CONSENT_LANGUAGE_MATCHES = "consent_language_matches"
    DPDP_PURPOSE_COVERS_ACTION = "dpdp_purpose_covers_action"

    # timing
    IS_WITHIN_QUIET_HOURS = "is_within_quiet_hours"
    QUIET_HOURS_END_AT = "quiet_hours_end_at"
    IS_DECLARED_HOLIDAY = "is_declared_holiday"
    HOURS_SINCE_LAST_LADDER_STEP = "hours_since_last_ladder_step"

    # frequency
    CONTACTS_ON_CHANNEL_LAST_7D = "contacts_on_channel_last_7d"
    CONTACTS_TOTAL_THIS_CASE = "contacts_total_this_case"
    HOURS_SINCE_LAST_CONTACT = "hours_since_last_contact"

    # rail & network
    MANDATE_IS_DEBITABLE = "mandate_is_debitable"
    MANDATE_CAP = "mandate_cap"
    DEBIT_AMOUNT = "debit_amount"
    HOURS_SINCE_PRE_DEBIT_NOTIFICATION = "hours_since_pre_debit_notification"
    PRE_DEBIT_NOTIFICATION_DELIVERED = "pre_debit_notification_delivered"
    NETWORK_RETRY_COUNT_THIS_WINDOW = "network_retry_count_this_window"
    CONSECUTIVE_HARD_DECLINES = "consecutive_hard_declines"
    LAST_DECLINE_WAS_HARD = "last_decline_was_hard"
    RAIL = "rail"

    # content
    TEMPLATE_IS_DLT_REGISTERED = "template_is_dlt_registered"
    TEMPLATE_LANGUAGE = "template_language"
    CONTAINS_BANNED_PHRASE = "contains_banned_phrase"
    HAS_FREE_TEXT_SLOT = "has_free_text_slot"
    DISCLOSES_AUTOMATED_CALL = "discloses_automated_call"

    # financial authority
    OBLIGATION_OUTSTANDING = "obligation_outstanding"
    CONCESSION_VALUE = "concession_value"
    PAYMENT_PLAN_INSTALMENT_COUNT = "payment_plan_instalment_count"
    GRACE_PERIOD_DAYS = "grace_period_days"

    # holds
    HAS_ACTIVE_HOLD = "has_active_hold"
    HAS_OPEN_DISPUTE = "has_open_dispute"
    HAS_HARDSHIP_FLAG = "has_hardship_flag"
    HAS_LEGAL_HOLD = "has_legal_hold"
    HAS_OPEN_CHARGEBACK = "has_open_chargeback"
    IN_SUPPRESSED_COHORT = "in_suppressed_cohort"
    HAS_OPEN_INCIDENT_ATTRIBUTABLE_TO_US = "has_open_incident_attributable_to_us"

    # integrity
    IDEMPOTENCY_KEY_ALREADY_USED = "idempotency_key_already_used"
    DUPLICATE_WITHIN_DEDUPE_WINDOW = "duplicate_within_dedupe_window"
    HAS_ACTIVE_REAUTH_LINK = "has_active_reauth_link"
    RECONCILIATION_IS_FRESH = "reconciliation_is_fresh"
    OBLIGATION_ALREADY_SETTLED = "obligation_already_settled"

    # context
    SEGMENT = "segment"
    IS_FIRST_CONTACT = "is_first_contact"
    DIAGNOSIS_CONFIDENCE = "diagnosis_confidence"
    FAILURE_CLASS_IS_NOVEL = "failure_class_is_novel"


FACT_TYPES: Mapping[PolicyFactKey, FactType] = {
    PolicyFactKey.HAS_CHANNEL_CONSENT: FactType.BOOLEAN,
    PolicyFactKey.CONSENT_RECORD_EXISTS: FactType.BOOLEAN,
    PolicyFactKey.IS_ON_DNC_LIST: FactType.BOOLEAN,
    PolicyFactKey.IS_OPTED_OUT: FactType.BOOLEAN,
    PolicyFactKey.CONSENT_LANGUAGE_MATCHES: FactType.BOOLEAN,
    PolicyFactKey.DPDP_PURPOSE_COVERS_ACTION: FactType.BOOLEAN,
    PolicyFactKey.IS_WITHIN_QUIET_HOURS: FactType.BOOLEAN,
    PolicyFactKey.QUIET_HOURS_END_AT: FactType.TIMESTAMP,
    PolicyFactKey.IS_DECLARED_HOLIDAY: FactType.BOOLEAN,
    PolicyFactKey.HOURS_SINCE_LAST_LADDER_STEP: FactType.DURATION_HOURS,
    PolicyFactKey.CONTACTS_ON_CHANNEL_LAST_7D: FactType.COUNT,
    PolicyFactKey.CONTACTS_TOTAL_THIS_CASE: FactType.COUNT,
    PolicyFactKey.HOURS_SINCE_LAST_CONTACT: FactType.DURATION_HOURS,
    PolicyFactKey.MANDATE_IS_DEBITABLE: FactType.BOOLEAN,
    PolicyFactKey.MANDATE_CAP: FactType.MONEY,
    PolicyFactKey.DEBIT_AMOUNT: FactType.MONEY,
    PolicyFactKey.HOURS_SINCE_PRE_DEBIT_NOTIFICATION: FactType.DURATION_HOURS,
    PolicyFactKey.PRE_DEBIT_NOTIFICATION_DELIVERED: FactType.BOOLEAN,
    PolicyFactKey.NETWORK_RETRY_COUNT_THIS_WINDOW: FactType.COUNT,
    PolicyFactKey.CONSECUTIVE_HARD_DECLINES: FactType.COUNT,
    PolicyFactKey.LAST_DECLINE_WAS_HARD: FactType.BOOLEAN,
    PolicyFactKey.RAIL: FactType.ENUM,
    PolicyFactKey.TEMPLATE_IS_DLT_REGISTERED: FactType.BOOLEAN,
    PolicyFactKey.TEMPLATE_LANGUAGE: FactType.ENUM,
    PolicyFactKey.CONTAINS_BANNED_PHRASE: FactType.BOOLEAN,
    PolicyFactKey.HAS_FREE_TEXT_SLOT: FactType.BOOLEAN,
    PolicyFactKey.DISCLOSES_AUTOMATED_CALL: FactType.BOOLEAN,
    PolicyFactKey.OBLIGATION_OUTSTANDING: FactType.MONEY,
    PolicyFactKey.CONCESSION_VALUE: FactType.MONEY,
    PolicyFactKey.PAYMENT_PLAN_INSTALMENT_COUNT: FactType.COUNT,
    PolicyFactKey.GRACE_PERIOD_DAYS: FactType.COUNT,
    PolicyFactKey.HAS_ACTIVE_HOLD: FactType.BOOLEAN,
    PolicyFactKey.HAS_OPEN_DISPUTE: FactType.BOOLEAN,
    PolicyFactKey.HAS_HARDSHIP_FLAG: FactType.BOOLEAN,
    PolicyFactKey.HAS_LEGAL_HOLD: FactType.BOOLEAN,
    PolicyFactKey.HAS_OPEN_CHARGEBACK: FactType.BOOLEAN,
    PolicyFactKey.IN_SUPPRESSED_COHORT: FactType.BOOLEAN,
    PolicyFactKey.HAS_OPEN_INCIDENT_ATTRIBUTABLE_TO_US: FactType.BOOLEAN,
    PolicyFactKey.IDEMPOTENCY_KEY_ALREADY_USED: FactType.BOOLEAN,
    PolicyFactKey.DUPLICATE_WITHIN_DEDUPE_WINDOW: FactType.BOOLEAN,
    PolicyFactKey.HAS_ACTIVE_REAUTH_LINK: FactType.BOOLEAN,
    PolicyFactKey.RECONCILIATION_IS_FRESH: FactType.BOOLEAN,
    PolicyFactKey.OBLIGATION_ALREADY_SETTLED: FactType.BOOLEAN,
    PolicyFactKey.SEGMENT: FactType.ENUM,
    PolicyFactKey.IS_FIRST_CONTACT: FactType.BOOLEAN,
    PolicyFactKey.DIAGNOSIS_CONFIDENCE: FactType.PROBABILITY,
    PolicyFactKey.FAILURE_CLASS_IS_NOVEL: FactType.BOOLEAN,
}

_missing_fact_types = set(PolicyFactKey) - set(FACT_TYPES)
if _missing_fact_types:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "FACT_TYPES is missing: " + ", ".join(sorted(f.value for f in _missing_fact_types))
    )


def fact_type(fact: PolicyFactKey) -> FactType:
    return FACT_TYPES[fact]


#: The enum an ENUM-typed fact's literals are checked against. Without it a
#: predicate could only ask "is this a string", and ``RAIL eq "card_emandat"``
#: is a string. Walked in both directions at import time below: a fact typed
#: ENUM with no vocabulary here, and a vocabulary for a fact that is not
#: ENUM-typed, are both startup errors rather than a rule that never fires.
ENUM_FACT_VOCABULARIES: Mapping[PolicyFactKey, type[Enum]] = {
    PolicyFactKey.RAIL: Rail,
    PolicyFactKey.TEMPLATE_LANGUAGE: Language,
    PolicyFactKey.SEGMENT: Segment,
}

_enum_facts = {f for f, t in FACT_TYPES.items() if t is FactType.ENUM}
if _enum_facts != set(ENUM_FACT_VOCABULARIES):  # pragma: no cover - import-time guard
    raise RuntimeError(
        "ENUM_FACT_VOCABULARIES disagrees with FACT_TYPES: missing "
        + str(sorted(f.value for f in _enum_facts - set(ENUM_FACT_VOCABULARIES)))
        + ", spurious "
        + str(sorted(f.value for f in set(ENUM_FACT_VOCABULARIES) - _enum_facts))
    )
del _enum_facts


def _reject_unknown_enum_literal(fact: PolicyFactKey, value: Any) -> None:
    """Raise unless ``value`` names a member of ``fact``'s own enum.

    A typo'd literal is not a rule that fails, it is a rule that *loads*: it
    passes its own allow/deny cases (which carry the same typo, or avoid the
    fact), it survives review because it reads correctly, and it never fires.
    The rule set then reads as coverage the engine does not have -- §6-2's defect
    class exactly, one fact type over.
    """
    vocabulary = ENUM_FACT_VOCABULARIES[fact]
    legal = sorted(str(member.value) for member in vocabulary)
    if not isinstance(value, str) or value not in legal:
        raise ValueError(
            f"{fact.value} is a {vocabulary.__name__}; {value!r} is not one of "
            + ", ".join(legal)
        )


class ComparisonOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"


_ORDERING_OPERATORS: Final[frozenset[ComparisonOperator]] = frozenset(
    {
        ComparisonOperator.GT,
        ComparisonOperator.GTE,
        ComparisonOperator.LT,
        ComparisonOperator.LTE,
    }
)

#: Fact types that support ordering comparisons.
_ORDERED_FACT_TYPES: Final[frozenset[FactType]] = frozenset(
    {FactType.MONEY, FactType.COUNT, FactType.DURATION_HOURS, FactType.TIMESTAMP, FactType.PROBABILITY}
)

#: A probability threshold inside a rule -- §14.2's "low confidence tiers up" is
#: the first of these. ``strict=True`` is load-bearing twice over: a float must not
#: become a Decimal by coercion (JC-15), and without it pydantic's union resolution
#: hands the number to ``UtcDatetime`` instead, silently turning a confidence
#: threshold of 0.55 into 1970-01-01T00:00:00.55Z -- a predicate that reads
#: correctly and matches everything.
_ProbabilityValue = Annotated[
    Decimal,
    Field(strict=True, ge=Decimal(0), le=Decimal(1)),
    AfterValidator(probability),
    PlainSerializer(fixed_point, return_type=str, when_used="json"),
]

_PredicateValue = Union[
    bool, int, str, Money, _ProbabilityValue, UtcDatetime, tuple[str, ...]
]


class FactPredicate(BaseModel):
    """A leaf test: one fact, one operator, one literal value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: Literal["fact"] = "fact"
    fact: PolicyFactKey
    operator: ComparisonOperator
    value: _PredicateValue

    @model_validator(mode="after")
    def _operator_and_value_suit_the_fact(self) -> "FactPredicate":
        ftype = FACT_TYPES[self.fact]
        if self.operator in _ORDERING_OPERATORS and ftype not in _ORDERED_FACT_TYPES:
            raise ValueError(
                f"{self.fact.value} is {ftype.value}; ordering operator "
                f"'{self.operator.value}' is meaningless on it"
            )
        if self.operator in (ComparisonOperator.IN, ComparisonOperator.NOT_IN):
            if not isinstance(self.value, tuple):
                raise ValueError(f"'{self.operator.value}' needs a tuple of values")
            if ftype is FactType.ENUM:
                # The membership form takes the same literals as the scalar form,
                # and `RAIL in (...)` is the natural way to write a multi-rail
                # rule. Checking only `eq` would leave the common case unchecked.
                for member in self.value:
                    _reject_unknown_enum_literal(self.fact, member)
            return self
        if ftype is FactType.MONEY and not isinstance(self.value, Money):
            raise ValueError(
                f"{self.fact.value} is money: compare it to a Money, not to a bare "
                "number. A bare number is ambiguous between rupees and paise, and "
                "that ambiguity is a 100x error"
            )
        if ftype is FactType.BOOLEAN and not isinstance(self.value, bool):
            raise ValueError(f"{self.fact.value} is boolean; got {type(self.value).__name__}")
        if ftype is FactType.PROBABILITY and not isinstance(self.value, Decimal):
            raise ValueError(
                f"{self.fact.value} is a probability: compare it to a Decimal in "
                f"[0, 1], not to a {type(self.value).__name__}. A float is refused "
                "by JC-15, and a bare string compares lexicographically -- "
                "'0.9' < '0.55' is False, so the rule would read as intended and "
                "evaluate backwards"
            )
        if ftype is FactType.TIMESTAMP and not isinstance(self.value, datetime):
            raise ValueError(
                f"{self.fact.value} is a timestamp: compare it to a timezone-aware "
                f"datetime, not to a {type(self.value).__name__}. An int satisfies "
                "the value union on its own, so `quiet_hours_end_at gte 5` used to "
                "load and compare an instant against the scalar 5; and a string "
                "compares lexicographically, which is right only for zero-padded "
                "RFC3339 -- '2026-9-05' < '2026-10-01' is False"
            )
        if ftype is FactType.ENUM:
            _reject_unknown_enum_literal(self.fact, self.value)
        if ftype in (FactType.COUNT, FactType.DURATION_HOURS):
            if isinstance(self.value, bool) or not isinstance(self.value, int):
                raise ValueError(f"{self.fact.value} is {ftype.value}; expected an int")
        return self


class AllOf(BaseModel):
    """Conjunction. Empty is rejected: a vacuous truth would match every case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: Literal["all_of"] = "all_of"
    all_of: tuple["Predicate", ...] = Field(min_length=1, max_length=32)


class AnyOf(BaseModel):
    """Disjunction. Empty is rejected: a vacuous falsehood is a dead rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: Literal["any_of"] = "any_of"
    any_of: tuple["Predicate", ...] = Field(min_length=1, max_length=32)


class Not(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node: Literal["not"] = "not"
    negate: "Predicate"


Predicate = Annotated[
    Union[FactPredicate, AllOf, AnyOf, Not],
    Field(discriminator="node"),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()
Not.model_rebuild()


def _predicate_depth(node: Any) -> int:
    if isinstance(node, FactPredicate):
        return 1
    if isinstance(node, AllOf):
        return 1 + max(_predicate_depth(c) for c in node.all_of)
    if isinstance(node, AnyOf):
        return 1 + max(_predicate_depth(c) for c in node.any_of)
    if isinstance(node, Not):
        return 1 + _predicate_depth(node.negate)
    raise TypeError(f"not a predicate node: {type(node).__name__}")


def _facts_referenced(node: Any) -> set[PolicyFactKey]:
    if isinstance(node, FactPredicate):
        return {node.fact}
    if isinstance(node, AllOf):
        return set().union(*(_facts_referenced(c) for c in node.all_of))
    if isinstance(node, AnyOf):
        return set().union(*(_facts_referenced(c) for c in node.any_of))
    if isinstance(node, Not):
        return _facts_referenced(node.negate)
    raise TypeError(f"not a predicate node: {type(node).__name__}")


# ------------------------------------------------------------------- rules

_RULE_ID = Annotated[str, StringConstraints(pattern=r"^POL-[A-Z0-9]{2,16}-\d{3,4}$")]
_NonEmpty = Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]


class RuleTestCase(BaseModel):
    """A rule's own allow/deny case. §14.1 requires these; making them a field
    means an untested rule cannot be loaded, rather than failing a waivable report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: _NonEmpty
    facts: Mapping[PolicyFactKey, Any]
    expect_matches: bool
    note: str = ""

    @field_validator("facts", mode="before")
    @classmethod
    def _facts_are_known(cls, facts: Any) -> Any:
        if isinstance(facts, Mapping):
            unknown = [k for k in facts if not isinstance(k, PolicyFactKey) and k not in {f.value for f in PolicyFactKey}]
            if unknown:
                raise ValueError(f"unknown policy facts in test case: {sorted(map(str, unknown))}")
        return facts


class PolicyRule(BaseModel):
    """One declarative rule. Data, not behaviour."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: _RULE_ID
    category: PolicyCategory
    title: _NonEmpty
    effect: PolicyEffect
    severity: RuleSeverity = RuleSeverity.BLOCKING
    when: Predicate = Field(description="Matches ⇒ the effect applies.")
    human_reason: str = Field(
        max_length=500,
        description="Shown in the approval queue and on the Recovery Receipt. An "
        "unexplained denial is unusable to the human who has to act on it.",
    )
    citation: _NonEmpty
    requires_tier: AutonomyTier | None = Field(
        default=None, description="Only meaningful for ALLOW_WITH_APPROVAL."
    )
    defer_until_fact: PolicyFactKey | None = Field(
        default=None, description="For DEFER: the timestamp fact to wait for."
    )
    applies_to_segments: tuple[Segment, ...] = Field(
        default=(), description="Empty means every segment."
    )
    applies_to_channels: tuple[Channel, ...] = Field(
        default=(), description="Empty means every channel."
    )
    test_cases: tuple[RuleTestCase, ...] = Field(min_length=2, max_length=32)
    enabled: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> "PolicyRule":
        if self.effect in (PolicyEffect.DENY, PolicyEffect.DEFER) and not self.human_reason.strip():
            raise ValueError(
                f"{self.rule_id}: a {self.effect.value} needs a human_reason; §14.1 "
                "verdicts are DENY(rule_id, human_reason)"
            )
        if self.effect is PolicyEffect.DEFER and self.defer_until_fact is None:
            raise ValueError(f"{self.rule_id}: DEFER(until) needs defer_until_fact")
        if self.defer_until_fact is not None:
            if FACT_TYPES[self.defer_until_fact] is not FactType.TIMESTAMP:
                raise ValueError(
                    f"{self.rule_id}: defer_until_fact must be a timestamp fact"
                )
        if self.effect is not PolicyEffect.ALLOW_WITH_APPROVAL and self.requires_tier is not None:
            raise ValueError(
                f"{self.rule_id}: requires_tier is only meaningful on "
                "ALLOW_WITH_APPROVAL; on any other effect it would silently do nothing"
            )
        if _predicate_depth(self.when) > MAX_PREDICATE_DEPTH:
            raise ValueError(
                f"{self.rule_id}: predicate nested deeper than {MAX_PREDICATE_DEPTH}; "
                "a rule a reviewer cannot read is a rule nobody has checked"
            )
        matching = [t for t in self.test_cases if t.expect_matches]
        non_matching = [t for t in self.test_cases if not t.expect_matches]
        if not matching or not non_matching:
            raise ValueError(
                f"{self.rule_id}: needs at least one matching and one non-matching "
                "test case. A rule with only positive cases has never been shown "
                "*not* to fire, which is how over-blocking ships"
            )
        return self

    @property
    def referenced_facts(self) -> frozenset[PolicyFactKey]:
        return frozenset(_facts_referenced(self.when))


class PolicyRuleSet(BaseModel):
    """A versioned, content-addressed set of rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]
    format_version: str = POLICY_FORMAT_VERSION
    rules: tuple[PolicyRule, ...] = Field(min_length=1)
    description: str = ""

    @model_validator(mode="after")
    def _unique_ids(self) -> "PolicyRuleSet":
        ids = [r.rule_id for r in self.rules]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate rule ids: {duplicates}")
        return self

    def by_category(self, category: PolicyCategory) -> tuple[PolicyRule, ...]:
        return tuple(r for r in self.rules if r.category is category)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def digest(self) -> str:
        """Content address. Catches an edited rule set that forgot to bump its
        version -- §15 records the policy version on every decision, and a version
        that no longer identifies its content makes attribution a lie."""
        return digest([r.model_dump(mode="json") for r in self.rules])


# ---------------------------------------------------------------- verdicts


class PolicyVerdict(BaseModel):
    """One rule's opinion about one proposed action. Logged even when it allows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str
    category: PolicyCategory
    effect: PolicyEffect
    human_reason: str = ""
    severity: RuleSeverity = RuleSeverity.BLOCKING
    requires_tier: AutonomyTier | None = None
    defer_until: UtcDatetime | None = None


#: JC-20: effects form a total order. Strictest wins, regardless of rule order.
_EFFECT_STRICTNESS: Final[Mapping[PolicyEffect, int]] = {
    PolicyEffect.ALLOW: 0,
    PolicyEffect.ALLOW_WITH_APPROVAL: 1,
    PolicyEffect.DEFER: 2,
    PolicyEffect.DENY: 3,
}


class PolicyDecision(BaseModel):
    """The combined outcome, with every verdict retained for the receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effect: PolicyEffect
    deciding_rule_id: str | None
    verdicts: tuple[PolicyVerdict, ...]
    advisory_rule_ids: tuple[str, ...] = ()
    requires_tier: AutonomyTier | None = None
    defer_until: UtcDatetime | None = None
    failed_closed: bool = Field(
        default=False,
        description="True when nothing evaluated. An action no rule permitted is "
        "not an action that is fine.",
    )
    policy_version: str | None = None

    @property
    def is_allowed(self) -> bool:
        return self.effect in (PolicyEffect.ALLOW, PolicyEffect.ALLOW_WITH_APPROVAL)


def combine_verdicts(
    verdicts: Sequence[PolicyVerdict], policy_version: str | None = None
) -> PolicyDecision:
    """Combine per-rule verdicts into one decision (JC-20, JC-21).

    Strictest blocking effect wins. Advisory verdicts are recorded but never block.
    An empty (or wholly advisory) input fails closed to DENY: §14.1 says conflicts
    fail closed, and "nothing evaluated" is the most complete failure there is.
    """
    ordered = tuple(verdicts)
    advisory = tuple(v.rule_id for v in ordered if v.severity is RuleSeverity.ADVISORY)
    blocking = [v for v in ordered if v.severity is RuleSeverity.BLOCKING]

    if not blocking:
        return PolicyDecision(
            effect=PolicyEffect.DENY,
            deciding_rule_id=None,
            verdicts=ordered,
            advisory_rule_ids=advisory,
            failed_closed=True,
            policy_version=policy_version,
        )

    strictest = max(_EFFECT_STRICTNESS[v.effect] for v in blocking)
    winners = [v for v in blocking if _EFFECT_STRICTNESS[v.effect] == strictest]
    effect = winners[0].effect

    # Deterministic tie-break by rule_id keeps the decision order-independent.
    deciding = min(winners, key=lambda v: v.rule_id)

    tiers = [v.requires_tier for v in blocking if v.requires_tier is not None]
    requires_tier = AutonomyTier.strictest(*tiers) if tiers else None

    defer_times = [v.defer_until for v in winners if v.defer_until is not None]
    # Two deferrals mean waiting for both, so the latest wins.
    defer_until = max(defer_times) if defer_times else None

    return PolicyDecision(
        effect=effect,
        deciding_rule_id=deciding.rule_id,
        verdicts=ordered,
        advisory_rule_ids=advisory,
        requires_tier=requires_tier,
        defer_until=defer_until,
        policy_version=policy_version,
    )


# -------------------------------------------------------------- thresholds

#: §14.2's grace-period authority matrix. A module-level table rather than an
#: inline ``default_factory`` body so it gets the same import-time totality guard
#: as ``FACT_TYPES``, ``RAIL_SPECS``, ``ACTION_SPECS``, ``PAYLOAD_MODELS`` and
#: ``DECLINE_CLASS_META``. Being a *field* default is precisely why the §6 sweep
#: for non-total enum-keyed mappings did not see this one (CONTRACTS.md §7 N1).
DEFAULT_MAX_GRACE_PERIOD_DAYS_BY_SEGMENT: Mapping[Segment, int] = {
    Segment.B2C_STANDARD: 7,
    Segment.B2C_PREMIUM: 14,
    Segment.B2B_SMB: 15,
    Segment.B2B_MID_MARKET: 30,
    Segment.B2B_ENTERPRISE: 45,
    Segment.B2B_STRATEGIC: 45,
}

_missing_grace_segments = set(Segment) - set(DEFAULT_MAX_GRACE_PERIOD_DAYS_BY_SEGMENT)
if _missing_grace_segments:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "DEFAULT_MAX_GRACE_PERIOD_DAYS_BY_SEGMENT is missing: "
        + ", ".join(sorted(s.value for s in _missing_grace_segments))
    )
del _missing_grace_segments

#: The zone the *configured* quiet-hours window is read in when a payer has none
#: of their own. ``PolicyThresholds`` carries clock times and no zone, so one had
#: to be chosen: RECLAIM is India-first, §14.1's default window (09:00-19:00) is
#: written in IST, and RBI's fair-practice contact norms assume it. The
#: alternative -- the server's zone -- would put invariant #3 ("no contact
#: outside quiet hours, in any timezone") one deploy-region change away from
#: being false, with nothing in the config to show it had moved.
FALLBACK_QUIET_HOURS_TIMEZONE: Final[str] = "Asia/Kolkata"


class PolicyThresholds(BaseModel):
    """The numbers *we* choose (JC-22). Loaded from YAML in Phase 1.

    Rail floors are not here -- they live in ``reclaim.contracts.rails`` and this
    configuration may only ever add caution on top of them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # financial authority
    t0_auto_reschedule_ceiling: Money = Field(
        default_factory=lambda: Money.from_rupees(2000),
        description="§14.2 T0: re-schedule a soft-declined debit at or below this "
        "amount without a human.",
    )
    afa_required_above: Money = Field(
        default_factory=lambda: Money.from_rupees(15000),
        description="§11.2 makes this explicitly config so an RBI change is a "
        "one-line edit rather than a code change.",
    )
    max_concession_value: Money = Field(
        default_factory=Money.zero,
        description="Invariant #7. Present, zero, and validated to stay zero: the "
        "reviewer should see the constraint in the config, not infer it from an "
        "absence.",
    )
    max_payment_plan_instalments: int = Field(default=6, ge=2, le=12)
    max_grace_period_days_by_segment: Mapping[Segment, int] = Field(
        default_factory=lambda: dict(DEFAULT_MAX_GRACE_PERIOD_DAYS_BY_SEGMENT),
        description="§14.2's grace-period authority matrix. Total over Segment, "
        "and validated below to stay total on every instance: an import-time "
        "guard can only see the default, and this field is loaded from YAML.",
    )

    # timing
    quiet_hours_start_local: time = Field(
        default=time(9, 0), description="§14.1 default 09:00 IST."
    )
    quiet_hours_end_local: time = Field(default=time(19, 0))
    min_hours_between_ladder_steps: int = Field(default=48, ge=1)
    pre_debit_notification_safety_margin_hours: int = Field(
        default=2,
        ge=0,
        description="Added to the rail's regulatory floor, never substituted. A "
        "misconfiguration can only make us notify earlier than required.",
    )

    # frequency
    max_contacts_per_channel_per_7d: int = Field(default=2, ge=0)
    max_contacts_total_per_case: int = Field(default=6, ge=0)
    dedupe_window_hours: int = Field(default=24, ge=0)

    # confidence
    diagnosis_confidence_floor: Annotated[str, StringConstraints(pattern=r"^0\.\d{1,6}$")] = Field(
        default="0.550000",
        description="Below this, the tier goes up (§14.2). Stored as a fixed-scale "
        "decimal string so YAML cannot introduce a float into a hashed config.",
    )

    @model_validator(mode="after")
    def _coherent(self) -> "PolicyThresholds":
        if self.max_concession_value != Money.zero():
            raise ValueError(
                "invariant #7: agent-granted concession value is Rs 0 and is not "
                "configurable. If a concession is genuinely needed, a human grants "
                "it in another system"
            )
        if self.quiet_hours_start_local >= self.quiet_hours_end_local:
            raise ValueError(
                "quiet_hours_start_local must be before quiet_hours_end_local; an "
                "inverted window silently permits contact at 03:00"
            )
        if self.t0_auto_reschedule_ceiling > self.afa_required_above:
            raise ValueError(
                "the T0 auto-reschedule ceiling cannot exceed the AFA threshold, or "
                "the agent would silently auto-debit amounts that require a human"
            )
        missing_segments = set(Segment) - set(self.max_grace_period_days_by_segment)
        if missing_segments:
            raise ValueError(
                "max_grace_period_days_by_segment must cover every segment; missing "
                + ", ".join(sorted(s.value for s in missing_segments))
                + ". The import-time guard on "
                "DEFAULT_MAX_GRACE_PERIOD_DAYS_BY_SEGMENT protects the default "
                "only; a YAML file that omits a segment would otherwise load "
                "cleanly and raise KeyError inside the policy engine, on the one "
                "case that carries the omitted segment"
            )
        for _label, _clock in (
            ("quiet_hours_start_local", self.quiet_hours_start_local),
            ("quiet_hours_end_local", self.quiet_hours_end_local),
        ):
            if (_clock.minute, _clock.second, _clock.microsecond) != (0, 0, 0):
                raise ValueError(
                    f"{_label}={_clock.isoformat()} is not on the hour. "
                    "resolve_quiet_hours maps this window onto QuietHours, which "
                    "the payer-side record expresses in whole local hours, so 09:30 "
                    "would be truncated to 09:00 -- silently widening the window we "
                    "may contact in by half an hour"
                )
        return self


# ------------------------------------------------------------- quiet hours


def resolve_quiet_hours(
    profile: ConsentProfile | None, thresholds: PolicyThresholds
) -> QuietHours:
    """Decide *which* quiet-hours window governs a payer (CONTRACTS.md §7 N7).

    §14.1 legitimately has two sources and no precedence between them: the
    payer's own window, which carries their IANA zone, and the configured global
    default, which carries clock times and no zone at all. Two sources with no
    rule is not a tie, it is whichever one the next caller happens to read --
    and invariant #3 is "no contact outside quiet hours, **in any timezone**".

    The rule, and the only place it is written:

    * the payer's ``quiet_hours`` wins whenever they have one, evaluated in its
      own ``timezone_name``. A stated preference outranks a default; anything
      else means asking someone their hours and then ignoring the answer.
    * otherwise the configured window, read as ``FALLBACK_QUIET_HOURS_TIMEZONE``.
    * no profile at all collapses into the same fallback. It is deliberately not
      a separate case: ``has_consent`` already returns False for a missing
      profile (§14.1 "absent consent record => no contact"), so the caller never
      reaches contact by this path, and a window is still needed by the
      ``QUIET_HOURS_END_AT`` fact a DEFER rule waits on.

    Returns the window rather than a yes/no, because answering "may I contact
    now?" needs a clock, and §12.5.4 keeps clocks out of the contract layer. The
    cost of that split is real: the engine still has to do the conversion, and
    this function cannot stop it doing it in the wrong zone -- it can only make
    sure there is exactly one zone to do it in.
    """
    if profile is not None and profile.quiet_hours is not None:
        return profile.quiet_hours
    return QuietHours(
        start_hour_local=thresholds.quiet_hours_start_local.hour,
        end_hour_local=thresholds.quiet_hours_end_local.hour,
        timezone_name=FALLBACK_QUIET_HOURS_TIMEZONE,
    )
