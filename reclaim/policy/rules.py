"""The minimal rule set: §14.1's eight categories, as data.

Phase 0 froze the rule *format* and shipped no rules. This is the smallest set
that is genuinely a policy engine rather than a demo: every category the two
governed verbs declare has a gate, each gate is a complementary DENY/ALLOW pair,
and every rule carries the allow+deny cases §14.1 requires -- which
``test_policy_rules.py`` executes rather than admires.

Why complementary pairs
-----------------------
``engine.evaluate`` denies when a declared ``policy_category`` produces no
blocking verdict. That is only workable if each category is guaranteed to
produce one, so each gate is authored as a single DENY condition plus an ALLOW
rule whose condition is literally ``Not(that condition)``. For any *total* fact
bundle exactly one of the pair matches, so a silent category means a missing
fact, never a gap between two hand-written conditions. The cost is that a gate
cannot express "allow only in these narrow circumstances, and stay silent
otherwise" -- it must say what it forbids, in full, and permit the complement.
For a compliance rule set that is the right shape anyway: the forbidden set is
the thing a reviewer needs to read.

Thresholds are configuration (§11.2, JC-22)
-------------------------------------------
``build_minimal_rule_set`` takes ``PolicyThresholds`` and interpolates the
numbers into the predicates, so the AFA threshold really is a one-line change
when RBI moves it. Two consequences worth stating:

* the rule *literal* is a snapshot of the config at build time -- a decision is
  attributable only if the ``PolicyRuleSet.digest``, not merely the
  ``policy_version``, is recorded with it. ``flow.py`` puts the digest into the
  ``policy_evaluated`` row's ``inputs_digest`` for exactly this reason.
* ``policy_version`` does **not** move on its own when a threshold changes. Two
  sets built from different thresholds share "1.0.0" and differ in digest. The
  version identifies the *rules*; the digest identifies the *decision*.

Rail floors are still not here. ``MAX_DEBIT_ATTEMPTS_PER_RECOVERY_WINDOW`` is a
number we chose, not a card-scheme limit we can cite, and it is named as such.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping, Sequence

from reclaim.contracts.actions import ACTION_SPECS, ActionType
from reclaim.contracts.enums import AutonomyTier, PolicyCategory, PolicyEffect
from reclaim.contracts.money import Money
from reclaim.contracts.policy_format import (
    AnyOf,
    ComparisonOperator as Op,
    FactPredicate,
    Not,
    PolicyFactKey as F,
    PolicyRule,
    PolicyRuleSet,
    PolicyThresholds,
    Predicate,
    RuleTestCase,
)

__all__ = [
    "GATED_CATEGORIES",
    "GOVERNED_ACTION_TYPES",
    "Gate",
    "MAX_DEBIT_ATTEMPTS_PER_RECOVERY_WINDOW",
    "MINIMAL_POLICY_VERSION",
    "MINIMAL_RULE_SET",
    "NOT_YET_ENCODED",
    "build_minimal_rule_set",
    "category_gate",
]

#: The verbs this set claims to govern. The router may emit no others: an action
#: type outside this tuple reaches the engine with a fact bundle assembled for a
#: different verb, and fails closed (a test pins that) -- correct, but it is a
#: refusal, not coverage.
GOVERNED_ACTION_TYPES: Final[tuple[ActionType, ...]] = (
    ActionType.SCHEDULE_DEBIT,
    ActionType.SEND_MESSAGE,
)

#: Our own cap on debit attempts inside one recovery window. **Not** a cited
#: card-network limit: §14.1 refers to "card-network excessive-retry limits" and
#: the plan's sources do not give a number, so this is a threshold we chose,
#: deliberately below any plausible scheme limit. Do not assert it to a judge as
#: a network rule (the ``rails.py`` ``verify_before_demo`` convention).
MAX_DEBIT_ATTEMPTS_PER_RECOVERY_WINDOW: Final[int] = 3

#: 1.1.0 -- additive: POL-FIN-001 changed from a flat DENY to
#: ALLOW_WITH_APPROVAL(T2) so an AFA-threshold debit parks in AWAITING_APPROVAL
#: instead of being refused (§14.2 T2, §9.1). The *format* is unchanged; the
#: shipped rule behaviour is not, so the version moves and every stored digest
#: with it.
MINIMAL_POLICY_VERSION: Final[str] = "1.1.0"

#: §14.1 clauses this set does **not** encode, and why. Written down as data so
#: that "we implement §14.1" is never claimed wholesale: each of these is a fact
#: the engine would have to invent, and a rule that reads a fact nobody computes
#: is coverage the engine does not have (CONTRACTS.md §6, defect class 2).
NOT_YET_ENCODED: Mapping[str, str] = {
    "frequency: >=48h between escalation ladder steps": (
        "needs a contact history; nothing in the spine records a sent contact yet, "
        "so hours_since_last_ladder_step would be a constant dressed as a fact"
    ),
    "integrity: dedupe window per (payer, channel, template)": (
        "same missing contact history"
    ),
    "integrity/reconciliation: the already-paid check before every contact (§14.3)": (
        "there is no reconciliation feed; obligation_already_settled below reads the "
        "ledger's obligation status, which is the ledger's opinion, not the bank's"
    ),
    "timing: declared holidays": (
        "is_declared_holiday is gated on, but the flow supplies False for every "
        "date -- there is no holiday calendar. The rule is real; the fact is not "
        "yet sourced"
    ),
    "content: banned-phrase check": (
        "contains_banned_phrase is gated on and is trivially False while every "
        "message is a registered template with no free-text slot. It becomes real "
        "the moment the LLM personalisation path lands"
    ),
    "autonomy tiers (§14.2): the deterministic tier resolver": (
        "the AFA-threshold slice is now encoded -- POL-FIN-001 returns "
        "ALLOW_WITH_APPROVAL(T2) and flow.py parks the case in AWAITING_APPROVAL. "
        "What is still missing is the general resolver that composes a tier from "
        "amount x reversibility x channel x customer tier x diagnosis confidence x "
        "novelty, and the other T2 triggers it would carry: first contact to an "
        "enterprise account, any voice call, any route/config change. Those need "
        "facts and verbs this set does not have yet"
    ),
}


# ---------------------------------------------------------------------------
# Gate construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    """One category's complementary pair, as loaded rules.

    ``restrictive`` is the half that fires on the forbidden condition -- a DENY
    for seven of the eight categories, and ALLOW_WITH_APPROVAL for financial
    authority, which routes an AFA-threshold debit to a human (§14.2 T2) rather
    than refusing it. ``permissive`` is always an ALLOW whose ``when`` is
    literally ``Not(restrictive.when)``. Naming the field ``restrictive`` rather
    than ``deny`` is the point: a field called ``deny`` holding an
    ALLOW_WITH_APPROVAL rule reads correctly and means the opposite.
    """

    category: PolicyCategory
    restrictive: PolicyRule
    permissive: PolicyRule


def _fact(key: F, operator: Op, value) -> FactPredicate:
    return FactPredicate(fact=key, operator=operator, value=value)


@dataclass(frozen=True)
class _GateSpec:
    """The authored half of a gate. The permissive ALLOW rule is derived from it.

    ``deny_when`` / ``deny_reason`` are the *restrictive* condition and its
    human-readable reason. They keep the ``deny_`` prefix because that is what
    they are for every gate except financial authority; ``restrictive_effect``
    is the one knob that turns a DENY into an ALLOW_WITH_APPROVAL, and
    ``restrictive_requires_tier`` is the tier that then rides on it (valid only
    on ALLOW_WITH_APPROVAL, per ``PolicyRule`` validation).
    """

    category: PolicyCategory
    deny_rule_id: str
    allow_rule_id: str
    subject: str
    deny_when: Predicate
    deny_reason: str
    allow_reason: str
    citation: str
    cases: tuple[RuleTestCase, ...]
    restrictive_effect: PolicyEffect = PolicyEffect.DENY
    restrictive_requires_tier: AutonomyTier | None = None


def _rules_for(spec: _GateSpec) -> tuple[PolicyRule, PolicyRule]:
    """The restrictive rule and its permissive complement.

    The permissive rule reuses the same test cases with ``expect_matches``
    inverted. That is not a shortcut: ``permissive.when`` *is*
    ``Not(restrictive.when)``, so a case that matches one matches the other
    exactly when the flag flips, and authoring them twice would only create
    somewhere for the two to disagree.

    ``spec.restrictive_effect`` is DENY for every gate except financial
    authority, which tiers up to a human (ALLOW_WITH_APPROVAL + T2) rather than
    forbidding the debit. Both effects are BLOCKING, so either one covers its
    category; the lattice (DENY > ALLOW_WITH_APPROVAL) still lets a hard stop on
    the same action win over the approval.
    """
    restrictive = PolicyRule(
        rule_id=spec.deny_rule_id,
        category=spec.category,
        title=(
            f"Deny: {spec.subject}"
            if spec.restrictive_effect is PolicyEffect.DENY
            else f"Human approval required: {spec.subject}"
        ),
        effect=spec.restrictive_effect,
        when=spec.deny_when,
        human_reason=spec.deny_reason,
        citation=spec.citation,
        requires_tier=spec.restrictive_requires_tier,
        test_cases=spec.cases,
    )
    permissive = PolicyRule(
        rule_id=spec.allow_rule_id,
        category=spec.category,
        title=f"Allow: {spec.subject}",
        effect=PolicyEffect.ALLOW,
        when=Not(negate=spec.deny_when),
        human_reason=spec.allow_reason,
        citation=spec.citation,
        test_cases=tuple(
            RuleTestCase(
                name=case.name,
                facts=case.facts,
                expect_matches=not case.expect_matches,
                note=case.note,
            )
            for case in spec.cases
        ),
    )
    return restrictive, permissive


def _case(name: str, facts: Mapping[F, object], *, matches: bool, note: str = ""):
    return RuleTestCase(
        name=name, facts=dict(facts), expect_matches=matches, note=note
    )


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def _financial_authority_gate(thresholds: PolicyThresholds) -> _GateSpec:
    """§14.1 Financial authority / §14.2 T2: an AFA-threshold debit is not ours
    to complete alone.

    Above the configured threshold RBI requires additional-factor authentication
    on *every* debit, which the agent cannot perform on the payer's behalf. The
    plan routes that to T2 -- human approval, not refusal -- so this gate returns
    ALLOW_WITH_APPROVAL carrying ``AutonomyTier.T2`` and ``flow.py`` parks the
    case in AWAITING_APPROVAL (§9.1). The lattice still lets a hard stop on the
    same debit (dispute, dead mandate, exhausted attempt budget) win: those are
    DENY, and DENY > ALLOW_WITH_APPROVAL.
    """
    ceiling = thresholds.afa_required_above
    return _GateSpec(
        category=PolicyCategory.FINANCIAL_AUTHORITY,
        deny_rule_id="POL-FIN-001",
        allow_rule_id="POL-FIN-002",
        subject="debit above the configured AFA threshold",
        restrictive_effect=PolicyEffect.ALLOW_WITH_APPROVAL,
        restrictive_requires_tier=AutonomyTier.T2,
        deny_when=_fact(F.DEBIT_AMOUNT, Op.GT, ceiling),
        deny_reason=(
            f"The debit is above the configured AFA threshold ({ceiling}). RBI "
            "requires additional-factor authentication on every recurring debit of "
            "this size, and the agent cannot complete it on the payer's behalf. "
            "Hold for a human to approve and drive the AFA journey."
        ),
        allow_reason=(
            f"At or below the configured AFA threshold ({ceiling}); no additional "
            "factor is required for this debit."
        ),
        citation=(
            "HACKATHON_PLAN.md §14.1 Rail/network, §14.2 T2, §11.2; Stripe India "
            "recurring docs: recurring transactions over Rs 15,000 must go through "
            "AFA each time."
        ),
        cases=(
            _case(
                "one rupee above the threshold",
                {F.DEBIT_AMOUNT: ceiling + Money.from_rupees(1)},
                matches=True,
            ),
            _case(
                "exactly at the threshold",
                {F.DEBIT_AMOUNT: ceiling},
                matches=False,
                note="the plan says 'over 15,000', so the boundary is allowed",
            ),
            _case(
                "a small debit",
                {F.DEBIT_AMOUNT: Money.from_rupees(499)},
                matches=False,
            ),
        ),
    )


_RAIL_CLEAR = {
    F.MANDATE_IS_DEBITABLE: True,
    F.LAST_DECLINE_WAS_HARD: False,
    F.NETWORK_RETRY_COUNT_THIS_WINDOW: 0,
}


def _rail_gate(thresholds: PolicyThresholds) -> _GateSpec:
    """§14.1 Rail/network, plus §14.3's hard-decline stop."""
    return _GateSpec(
        category=PolicyCategory.RAIL_AND_NETWORK,
        deny_rule_id="POL-RAIL-001",
        allow_rule_id="POL-RAIL-002",
        subject="debit the rail or the network will not accept",
        deny_when=AnyOf(
            any_of=(
                _fact(F.MANDATE_IS_DEBITABLE, Op.EQ, False),
                _fact(F.LAST_DECLINE_WAS_HARD, Op.EQ, True),
                _fact(
                    F.NETWORK_RETRY_COUNT_THIS_WINDOW,
                    Op.GTE,
                    MAX_DEBIT_ATTEMPTS_PER_RECOVERY_WINDOW,
                ),
            )
        ),
        deny_reason=(
            "No debit: the mandate is not in a debitable state, or the last decline "
            "was hard (retrying a hard decline has zero probability until the "
            "instrument or the authorisation changes), or this obligation has "
            f"already used its {MAX_DEBIT_ATTEMPTS_PER_RECOVERY_WINDOW} attempts in "
            "this recovery window."
        ),
        allow_reason=(
            "The mandate is debitable, the last decline was soft, and the attempt "
            "budget for this recovery window is not exhausted."
        ),
        citation=(
            "HACKATHON_PLAN.md §14.1 Rail/network ('no debit on an "
            "invalid/cancelled/paused mandate'; card-network excessive-retry "
            "limits), §14.3 ('hard decline => zero further debits'); Stripe: "
            "authentication_required is a hard decline."
        ),
        cases=(
            _case("mandate not debitable", {**_RAIL_CLEAR, F.MANDATE_IS_DEBITABLE: False}, matches=True),
            _case("last decline was hard", {**_RAIL_CLEAR, F.LAST_DECLINE_WAS_HARD: True}, matches=True),
            _case(
                "attempt budget exhausted",
                {
                    **_RAIL_CLEAR,
                    F.NETWORK_RETRY_COUNT_THIS_WINDOW: MAX_DEBIT_ATTEMPTS_PER_RECOVERY_WINDOW,
                },
                matches=True,
            ),
            _case("live mandate, soft decline, first retry", dict(_RAIL_CLEAR), matches=False),
        ),
    )


_HOLD_FACTS: Final[tuple[F, ...]] = (
    F.IS_OPTED_OUT,
    F.HAS_ACTIVE_HOLD,
    F.HAS_OPEN_DISPUTE,
    F.HAS_HARDSHIP_FLAG,
    F.HAS_LEGAL_HOLD,
    F.HAS_OPEN_CHARGEBACK,
    F.IN_SUPPRESSED_COHORT,
    F.HAS_OPEN_INCIDENT_ATTRIBUTABLE_TO_US,
)

_HOLDS_CLEAR = {fact: False for fact in _HOLD_FACTS}


def _holds_gate(thresholds: PolicyThresholds) -> _GateSpec:
    """§14.1 Holds -- the immediate hard stops. Every one of these blocks *both*
    contact and money movement, which is why the category is on both verbs."""
    return _GateSpec(
        category=PolicyCategory.HOLDS,
        deny_rule_id="POL-HOLDS-001",
        allow_rule_id="POL-HOLDS-002",
        subject="any open hard stop on the payer or the obligation",
        deny_when=AnyOf(
            any_of=tuple(_fact(fact, Op.EQ, True) for fact in _HOLD_FACTS)
        ),
        deny_reason=(
            "An immediate hard stop is open on this payer or obligation (opt-out, "
            "dispute, hardship, legal hold, chargeback, suppressed cohort, or an "
            "open incident attributable to us). Nothing may be sent and nothing may "
            "be debited while it stands."
        ),
        allow_reason="No hard stop is open on this payer or obligation.",
        citation="HACKATHON_PLAN.md §14.1 Holds; §14.3 stopping rules; invariant #2.",
        cases=(
            _case("payer has opted out", {**_HOLDS_CLEAR, F.IS_OPTED_OUT: True}, matches=True),
            _case("open dispute", {**_HOLDS_CLEAR, F.HAS_OPEN_DISPUTE: True}, matches=True),
            _case(
                "our own incident suppresses the cohort",
                {**_HOLDS_CLEAR, F.HAS_OPEN_INCIDENT_ATTRIBUTABLE_TO_US: True},
                matches=True,
            ),
            _case("no hold of any kind", dict(_HOLDS_CLEAR), matches=False),
        ),
    )


_INTEGRITY_CLEAR = {
    F.IDEMPOTENCY_KEY_ALREADY_USED: False,
    F.OBLIGATION_ALREADY_SETTLED: False,
}


def _integrity_gate(thresholds: PolicyThresholds) -> _GateSpec:
    return _GateSpec(
        category=PolicyCategory.INTEGRITY,
        deny_rule_id="POL-INTEG-001",
        allow_rule_id="POL-INTEG-002",
        subject="action already taken, or an obligation that is no longer owed",
        deny_when=AnyOf(
            any_of=(
                _fact(F.IDEMPOTENCY_KEY_ALREADY_USED, Op.EQ, True),
                _fact(F.OBLIGATION_ALREADY_SETTLED, Op.EQ, True),
            )
        ),
        deny_reason=(
            "This exact action has already been enqueued under its idempotency key, "
            "or the obligation is no longer outstanding. Acting again would either "
            "double-execute or chase money that is not owed."
        ),
        allow_reason=(
            "The action is new under its idempotency key and the obligation is still "
            "outstanding."
        ),
        citation=(
            "HACKATHON_PLAN.md §14.1 Integrity; §14.3 ('customer already paid'); "
            "invariants #1 and #8."
        ),
        cases=(
            _case(
                "idempotency key already used",
                {**_INTEGRITY_CLEAR, F.IDEMPOTENCY_KEY_ALREADY_USED: True},
                matches=True,
            ),
            _case(
                "obligation already settled",
                {**_INTEGRITY_CLEAR, F.OBLIGATION_ALREADY_SETTLED: True},
                matches=True,
            ),
            _case("new action on an open obligation", dict(_INTEGRITY_CLEAR), matches=False),
        ),
    )


_CONSENT_CLEAR = {
    F.IS_OPTED_OUT: False,
    F.IS_ON_DNC_LIST: False,
    F.CONSENT_RECORD_EXISTS: True,
    F.HAS_CHANNEL_CONSENT: True,
    F.DPDP_PURPOSE_COVERS_ACTION: True,
    F.CONSENT_LANGUAGE_MATCHES: True,
}


def _consent_gate(thresholds: PolicyThresholds) -> _GateSpec:
    """§14.1 Consent & channel. "Absent consent record => no contact" is the
    clause that makes this gate deny by *default* rather than on a positive
    signal: ``CONSENT_RECORD_EXISTS eq False`` is a denial condition."""
    return _GateSpec(
        category=PolicyCategory.CONSENT_AND_CHANNEL,
        deny_rule_id="POL-CONSENT-001",
        allow_rule_id="POL-CONSENT-002",
        subject="contact without effective, purpose-covered consent on this channel",
        deny_when=AnyOf(
            any_of=(
                _fact(F.IS_OPTED_OUT, Op.EQ, True),
                _fact(F.IS_ON_DNC_LIST, Op.EQ, True),
                _fact(F.CONSENT_RECORD_EXISTS, Op.EQ, False),
                _fact(F.HAS_CHANNEL_CONSENT, Op.EQ, False),
                _fact(F.DPDP_PURPOSE_COVERS_ACTION, Op.EQ, False),
                _fact(F.CONSENT_LANGUAGE_MATCHES, Op.EQ, False),
            )
        ),
        deny_reason=(
            "No contact: the payer has opted out or is on the DNC list, or there is "
            "no effective consent record for this channel, or the DPDP purpose this "
            "consent was captured for does not cover this contact, or the message "
            "language does not match the consented language. An absent consent "
            "record is a denial, not an unknown."
        ),
        allow_reason=(
            "Effective, purpose-covered consent exists for this channel in the "
            "payer's consented language."
        ),
        citation=(
            "HACKATHON_PLAN.md §14.1 Consent & channel ('absent consent record => "
            "no contact'; DPDP purpose limitation; DNC); §10.1 "
            "get_consent_profile unavailable => treat as no consent; invariant #2."
        ),
        cases=(
            _case("payer opted out", {**_CONSENT_CLEAR, F.IS_OPTED_OUT: True}, matches=True),
            _case("on the DNC list", {**_CONSENT_CLEAR, F.IS_ON_DNC_LIST: True}, matches=True),
            _case(
                "no consent record at all",
                {**_CONSENT_CLEAR, F.CONSENT_RECORD_EXISTS: False, F.HAS_CHANNEL_CONSENT: False},
                matches=True,
            ),
            _case(
                "consented, but for a different DPDP purpose",
                {**_CONSENT_CLEAR, F.DPDP_PURPOSE_COVERS_ACTION: False},
                matches=True,
            ),
            _case("fully consented on this channel", dict(_CONSENT_CLEAR), matches=False),
        ),
    )


_TIMING_CLEAR = {
    F.IS_WITHIN_QUIET_HOURS: True,
    F.IS_DECLARED_HOLIDAY: False,
}


def _timing_gate(thresholds: PolicyThresholds) -> _GateSpec:
    """§14.1 Timing -- invariant #3, "no contact outside quiet hours, in any
    timezone".

    ``IS_WITHIN_QUIET_HOURS`` is computed in ``facts.py`` from
    ``policy_format.resolve_quiet_hours``, which is the only place the precedence
    between a payer's stated window and the configured default is decided (JC-43).
    This gate deliberately does not read either source itself: a second reading
    would be a second rule.
    """
    return _GateSpec(
        category=PolicyCategory.TIMING,
        deny_rule_id="POL-TIME-001",
        allow_rule_id="POL-TIME-002",
        subject="contact outside the contact window that governs this payer",
        deny_when=AnyOf(
            any_of=(
                _fact(F.IS_WITHIN_QUIET_HOURS, Op.EQ, False),
                _fact(F.IS_DECLARED_HOLIDAY, Op.EQ, True),
            )
        ),
        deny_reason=(
            "Outside the contact window that governs this payer (their stated hours "
            "if they have any, otherwise the configured default read in "
            "Asia/Kolkata), or on a declared holiday. Invariant #3 holds in the "
            "payer's timezone, not the server's."
        ),
        allow_reason=(
            "Inside the contact window that governs this payer, on a non-holiday."
        ),
        citation=(
            "HACKATHON_PLAN.md §14.1 Timing (quiet hours, default 09:00-19:00 IST; "
            "no contact on declared holidays); invariant #3; JC-43."
        ),
        cases=(
            _case(
                "the local clock is outside the window",
                {**_TIMING_CLEAR, F.IS_WITHIN_QUIET_HOURS: False},
                matches=True,
            ),
            _case(
                "inside the window but a declared holiday",
                {**_TIMING_CLEAR, F.IS_DECLARED_HOLIDAY: True},
                matches=True,
            ),
            _case("a working afternoon in the payer's zone", dict(_TIMING_CLEAR), matches=False),
        ),
    )


def _frequency_gate(thresholds: PolicyThresholds) -> _GateSpec:
    per_channel = thresholds.max_contacts_per_channel_per_7d
    per_case = thresholds.max_contacts_total_per_case
    clear = {
        F.CONTACTS_ON_CHANNEL_LAST_7D: 0,
        F.CONTACTS_TOTAL_THIS_CASE: 0,
    }
    return _GateSpec(
        category=PolicyCategory.FREQUENCY,
        deny_rule_id="POL-FREQ-001",
        allow_rule_id="POL-FREQ-002",
        subject="contact beyond the frequency caps",
        deny_when=AnyOf(
            any_of=(
                _fact(F.CONTACTS_ON_CHANNEL_LAST_7D, Op.GTE, per_channel),
                _fact(F.CONTACTS_TOTAL_THIS_CASE, Op.GTE, per_case),
            )
        ),
        deny_reason=(
            f"Frequency cap reached: at most {per_channel} contacts per channel per "
            f"rolling 7 days and {per_case} in total per case. A recovery ladder "
            "that ignores its own cap is harassment with a schema."
        ),
        allow_reason=(
            f"Within both frequency caps ({per_channel} per channel per 7 days, "
            f"{per_case} per case)."
        ),
        citation="HACKATHON_PLAN.md §14.1 Frequency; §14.3 ('contact cap reached').",
        cases=(
            _case(
                "channel cap reached",
                {**clear, F.CONTACTS_ON_CHANNEL_LAST_7D: per_channel},
                matches=True,
            ),
            _case(
                "case cap reached",
                {**clear, F.CONTACTS_TOTAL_THIS_CASE: per_case},
                matches=True,
            ),
            _case("first contact on this case", dict(clear), matches=False),
        ),
    )


_CONTENT_CLEAR = {
    F.CONTAINS_BANNED_PHRASE: False,
    F.HAS_FREE_TEXT_SLOT: False,
    F.TEMPLATE_IS_DLT_REGISTERED: True,
}


def _content_gate(thresholds: PolicyThresholds) -> _GateSpec:
    """§14.1 Content.

    DLT registration is required here on *every* channel, not only SMS where TRAI
    mandates it. That is stricter than the regulation and is a deliberate
    simplification: this set has one template registry, and a template nobody
    registered is a template nobody reviewed.
    """
    return _GateSpec(
        category=PolicyCategory.CONTENT,
        deny_rule_id="POL-CONTENT-001",
        allow_rule_id="POL-CONTENT-002",
        subject="message whose content has not been through the registry",
        deny_when=AnyOf(
            any_of=(
                _fact(F.CONTAINS_BANNED_PHRASE, Op.EQ, True),
                _fact(F.HAS_FREE_TEXT_SLOT, Op.EQ, True),
                _fact(F.TEMPLATE_IS_DLT_REGISTERED, Op.EQ, False),
            )
        ),
        deny_reason=(
            "The message is not sendable as composed: it uses an unregistered "
            "template, carries a free-text slot, or contains a banned phrase "
            "(threats, legal claims we cannot make, third-party disclosure, implied "
            "credit-bureau consequence)."
        ),
        allow_reason=(
            "A registered template with no free-text slot and no banned phrase."
        ),
        citation=(
            "HACKATHON_PLAN.md §14.1 Content (DLT-registered templates for SMS; "
            "banned-phrase check); JC-17 (send_message has no body, only named "
            "slots in a registered template)."
        ),
        cases=(
            _case(
                "unregistered template",
                {**_CONTENT_CLEAR, F.TEMPLATE_IS_DLT_REGISTERED: False},
                matches=True,
            ),
            _case(
                "free prose in a slot",
                {**_CONTENT_CLEAR, F.HAS_FREE_TEXT_SLOT: True},
                matches=True,
            ),
            _case(
                "banned phrase",
                {**_CONTENT_CLEAR, F.CONTAINS_BANNED_PHRASE: True},
                matches=True,
            ),
            _case("registered template, named slots only", dict(_CONTENT_CLEAR), matches=False),
        ),
    )


#: The gate builders, in the order their rules are emitted. Keyed by category so
#: the totality check below walks the same structure the engine iterates.
_GATE_BUILDERS = (
    _consent_gate,
    _timing_gate,
    _frequency_gate,
    _rail_gate,
    _content_gate,
    _financial_authority_gate,
    _holds_gate,
    _integrity_gate,
)

#: Every category this set gates. Derived from the builders rather than restated,
#: so a gate added without a category (or a category listed with no gate) cannot
#: happen.
GATED_CATEGORIES: Final[tuple[PolicyCategory, ...]] = tuple(
    build(PolicyThresholds()).category for build in _GATE_BUILDERS
)

_required_categories = set()
for _action_type in GOVERNED_ACTION_TYPES:
    _required_categories |= ACTION_SPECS[_action_type].policy_categories
_uncovered = _required_categories - set(GATED_CATEGORIES)
if _uncovered:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "the minimal rule set claims to govern "
        + ", ".join(t.value for t in GOVERNED_ACTION_TYPES)
        + " but has no gate for: "
        + ", ".join(sorted(c.value for c in _uncovered))
        + ". Every declared policy_category must have a gate or the engine denies "
        "every such action."
    )
del _required_categories, _uncovered, _action_type


def build_minimal_rule_set(
    thresholds: PolicyThresholds | None = None,
    *,
    policy_version: str = MINIMAL_POLICY_VERSION,
) -> PolicyRuleSet:
    """Build the rule set, interpolating ``thresholds`` into the predicates."""
    resolved = thresholds if thresholds is not None else PolicyThresholds()
    rules: list[PolicyRule] = []
    for build in _GATE_BUILDERS:
        rules.extend(_rules_for(build(resolved)))
    return PolicyRuleSet(
        policy_version=policy_version,
        rules=tuple(rules),
        description=(
            "RECLAIM minimal policy set: one complementary restrictive/ALLOW gate "
            "per §14.1 category declared by schedule_debit and send_message. Seven "
            "gates deny; financial authority tiers up to a human (§14.2 T2). "
            "Thresholds are interpolated from PolicyThresholds at build time, so "
            "the digest -- not the version -- identifies a decision."
        ),
    )


MINIMAL_RULE_SET: Final[PolicyRuleSet] = build_minimal_rule_set()


def category_gate(rule_set: PolicyRuleSet, category: PolicyCategory) -> Gate:
    """The restrictive/permissive pair a rule set carries for ``category``.

    Raises unless the category holds exactly one permissive ALLOW and one
    restrictive rule (a DENY, or ALLOW_WITH_APPROVAL for financial authority):
    the complementarity property this set is built on is not defined otherwise.
    """
    in_category = rule_set.by_category(category)
    permissive = [r for r in in_category if r.effect is PolicyEffect.ALLOW]
    restrictive = [r for r in in_category if r.effect is not PolicyEffect.ALLOW]
    if len(restrictive) != 1 or len(permissive) != 1 or len(in_category) != 2:
        raise ValueError(
            f"{category.value} is not a complementary gate: "
            f"{len(restrictive)} restrictive, {len(permissive)} permissive, "
            f"{len(in_category)} total"
        )
    return Gate(
        category=category, restrictive=restrictive[0], permissive=permissive[0]
    )
