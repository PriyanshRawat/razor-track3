"""Phase 1: the shipped minimal rule set.

§14.1 says every rule ships with an allow test and a deny test, and
``PolicyRule.test_cases`` makes that a *field* so an untested rule cannot load.
A field is only half the promise: nothing in Phase 0 ever **runs** those cases.
``test_every_rule_passes_its_own_declared_test_cases`` is the other half, and it
is the reason a rule here can be trusted to fire.

The rest of this file pins the three structural properties the set is built on:

* **Complementary pairs.** Each category ships one DENY rule and one ALLOW rule
  whose condition is literally ``Not(the deny condition)``. That is what makes
  "every declared category returns a verdict" total rather than a claim -- there
  is no fact bundle for which both are silent. The test asserts the structure,
  not a sample of inputs.
* **Thresholds are config.** §11.2 promises the AFA threshold is a one-line
  change. If the literal in ``POL-FIN-001`` did not move with
  ``PolicyThresholds``, that promise would be prose.
* **The set governs only what it claims to.** An action type outside
  ``GOVERNED_ACTION_TYPES`` must fail closed, not sail through on rules written
  for a different verb.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from reclaim.contracts.actions import (
    ACTION_SPECS,
    ActionEnvelope,
    ActionType,
    OfferPaymentPlan,
    PaymentPlanInstalment,
    ScheduleDebit,
)
from reclaim.contracts.enums import (
    AutonomyTier,
    PlanOrigin,
    PolicyCategory,
    PolicyEffect,
    Rail,
    RuleSeverity,
    Segment,
)
from reclaim.contracts.money import Money
from reclaim.contracts.policy_format import Not, PolicyThresholds
from reclaim.policy import rules as rules_module
from reclaim.policy.engine import evaluate, matches
from reclaim.policy.rules import (
    GOVERNED_ACTION_TYPES,
    MINIMAL_RULE_SET,
    build_minimal_rule_set,
    category_gate,
)

TS = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)


def _debit(amount: Money, **over) -> ActionEnvelope:
    return ActionEnvelope(
        action_id=over.pop("action_id", "act_1"),
        case_id=over.pop("case_id", "case_1"),
        action=ScheduleDebit(
            obligation_id="obl_1",
            rail=Rail.CARD_ONE_TIME,
            amount=amount,
            execute_at=TS + timedelta(days=1),
            attempt_sequence=2,
        ),
        proposed_by=PlanOrigin.DETERMINISTIC_ROUTER,
    )


def _clean_debit_facts(amount: Money) -> dict:
    """A fact bundle in which every debit gate allows. Built by hand here so the
    rule tests do not depend on ``facts.py``."""
    from reclaim.contracts.policy_format import PolicyFactKey as F

    return {
        F.DEBIT_AMOUNT: amount,
        F.MANDATE_IS_DEBITABLE: True,
        F.LAST_DECLINE_WAS_HARD: False,
        F.NETWORK_RETRY_COUNT_THIS_WINDOW: 0,
        F.IS_OPTED_OUT: False,
        F.HAS_ACTIVE_HOLD: False,
        F.HAS_OPEN_DISPUTE: False,
        F.HAS_HARDSHIP_FLAG: False,
        F.HAS_LEGAL_HOLD: False,
        F.HAS_OPEN_CHARGEBACK: False,
        F.IN_SUPPRESSED_COHORT: False,
        F.HAS_OPEN_INCIDENT_ATTRIBUTABLE_TO_US: False,
        F.IDEMPOTENCY_KEY_ALREADY_USED: False,
        F.OBLIGATION_ALREADY_SETTLED: False,
    }


# --------------------------------------------- the rules ship with their tests


def test_every_rule_passes_its_own_declared_test_cases():
    """§14.1's per-rule allow+deny tests, executed rather than merely declared.

    A rule whose predicate reads correctly but never fires passes review, passes
    ``PolicyRule`` validation, and covers nothing (CONTRACTS.md §6, defect 2's
    class). This is the test that would catch it.
    """
    for rule in MINIMAL_RULE_SET.rules:
        for case in rule.test_cases:
            assert matches(rule.when, case.facts) is case.expect_matches, (
                f"{rule.rule_id} test case {case.name!r} expected "
                f"expect_matches={case.expect_matches}"
            )


def test_every_rule_declares_a_citation_and_a_reason_a_human_can_read():
    for rule in MINIMAL_RULE_SET.rules:
        assert rule.citation.strip(), rule.rule_id
        assert rule.human_reason.strip(), rule.rule_id


# ------------------------------------------------------- structural coverage


def test_every_category_the_governed_actions_declare_has_a_gate():
    """``ActionSpec.policy_categories`` is what the engine iterates. A category a
    governed action declares but the rule set omits is a fail-closed DENY on every
    such action -- correct, but useless."""
    required: set[PolicyCategory] = set()
    for action_type in GOVERNED_ACTION_TYPES:
        required |= ACTION_SPECS[action_type].policy_categories
    assert required == set(rules_module.GATED_CATEGORIES)


#: The effects a gate's restrictive half may carry. DENY for seven gates; the
#: financial-authority gate tiers up to a human instead (§14.2 T2), so it is
#: ALLOW_WITH_APPROVAL. Both are BLOCKING and both cover their category.
_RESTRICTIVE_EFFECTS = frozenset(
    {PolicyEffect.DENY, PolicyEffect.ALLOW_WITH_APPROVAL}
)


def test_each_gate_is_a_complementary_restrictive_allow_pair():
    """The permissive rule of a gate is exactly ``Not`` of its restrictive
    condition.

    This is the structural reason a declared category can never be silent: for any
    total fact bundle exactly one of the pair matches. Asserting the *shape*
    rather than sampling inputs is the point -- a sampled proof would pass on the
    day someone loosens the allow condition by an ``or``.
    """
    for category in rules_module.GATED_CATEGORIES:
        gate = category_gate(MINIMAL_RULE_SET, category)
        assert gate.restrictive.effect in _RESTRICTIVE_EFFECTS
        assert gate.permissive.effect is PolicyEffect.ALLOW
        assert gate.restrictive.severity is RuleSeverity.BLOCKING
        assert gate.permissive.severity is RuleSeverity.BLOCKING
        assert gate.permissive.when == Not(negate=gate.restrictive.when)
        assert gate.permissive.category is category is gate.restrictive.category


def test_only_the_finance_gate_tiers_up_instead_of_denying():
    """Seven of the eight gates forbid outright; financial authority is the one
    that routes to a human (§14.2 T2) rather than refusing."""
    for category in rules_module.GATED_CATEGORIES:
        gate = category_gate(MINIMAL_RULE_SET, category)
        if category is PolicyCategory.FINANCIAL_AUTHORITY:
            assert gate.restrictive.effect is PolicyEffect.ALLOW_WITH_APPROVAL
        else:
            assert gate.restrictive.effect is PolicyEffect.DENY


def test_no_rule_is_shipped_disabled():
    """A disabled rule is coverage the engine does not have. If a rule should not
    run, delete it and bump the version."""
    assert all(rule.enabled for rule in MINIMAL_RULE_SET.rules)


# ---------------------------------------------------- thresholds are config


def test_the_afa_gate_reads_its_threshold_from_configuration():
    """§11.2: "the AFA threshold is a config value, so when RBI moves it, we move
    one line". The literal in the rule must actually move with it."""
    relaxed = PolicyThresholds(afa_required_above=Money.from_rupees(100000))
    gate = category_gate(
        build_minimal_rule_set(relaxed), PolicyCategory.FINANCIAL_AUTHORITY
    )
    assert gate.restrictive.when.value == Money.from_rupees(100000)


def test_a_threshold_change_changes_the_rule_sets_digest():
    """``PolicyRuleSet.digest`` is the content address §15 attributes decisions
    to. Two sets that decide differently must not share one."""
    relaxed = PolicyThresholds(afa_required_above=Money.from_rupees(100000))
    assert build_minimal_rule_set(relaxed).digest != MINIMAL_RULE_SET.digest


def test_a_debit_above_the_afa_threshold_needs_human_approval_not_a_denial():
    """§14.2 T2: a recurring debit above the configured AFA threshold cannot be
    completed by the agent, but it is not refused either -- it is routed to a
    human. The finance gate returns ALLOW_WITH_APPROVAL carrying the T2 tier."""
    thresholds = PolicyThresholds()
    above = thresholds.afa_required_above + Money.from_rupees(1)
    result = evaluate(
        MINIMAL_RULE_SET,
        _debit(above),
        _clean_debit_facts(above),
        segment=Segment.B2C_STANDARD,
    )
    assert result.decision.effect is PolicyEffect.ALLOW_WITH_APPROVAL
    assert result.decision.deciding_rule_id == "POL-FIN-001"
    assert result.decision.requires_tier is AutonomyTier.T2
    assert result.decision.is_allowed is True
    assert result.decision.failed_closed is False


def test_a_stricter_deny_still_beats_the_afa_approval_on_a_big_debit():
    """The lattice is DENY > ALLOW_WITH_APPROVAL: a high-value debit that also
    trips a hard stop is denied, not queued. No money waits on a human for an
    action a hold already forbids."""
    from reclaim.contracts.policy_format import PolicyFactKey as F

    thresholds = PolicyThresholds()
    above = thresholds.afa_required_above + Money.from_rupees(1)
    facts = _clean_debit_facts(above)
    facts[F.HAS_OPEN_DISPUTE] = True
    result = evaluate(
        MINIMAL_RULE_SET, _debit(above), facts, segment=Segment.B2C_STANDARD
    )
    assert result.decision.effect is PolicyEffect.DENY
    assert result.decision.deciding_rule_id == "POL-HOLDS-001"


def test_the_afa_gate_restrictive_rule_carries_the_t2_tier():
    """``requires_tier`` is only meaningful on ALLOW_WITH_APPROVAL, and the
    approval queue needs to know which tier signed off. The complement carries
    none."""
    gate = category_gate(MINIMAL_RULE_SET, PolicyCategory.FINANCIAL_AUTHORITY)
    assert gate.restrictive.requires_tier is AutonomyTier.T2
    assert gate.permissive.requires_tier is None


def test_a_debit_at_the_afa_threshold_is_allowed():
    thresholds = PolicyThresholds()
    at = thresholds.afa_required_above
    result = evaluate(
        MINIMAL_RULE_SET,
        _debit(at),
        _clean_debit_facts(at),
        segment=Segment.B2C_STANDARD,
    )
    assert result.decision.effect is PolicyEffect.ALLOW
    assert result.silent_categories == ()
    assert result.unevaluable_rule_ids == ()


def test_a_hard_decline_denies_a_further_debit():
    """§14.3: a hard decline permits *zero* further debits until something about
    the instrument or the authorisation changes."""
    amount = Money.from_rupees(1499)
    facts = _clean_debit_facts(amount)
    from reclaim.contracts.policy_format import PolicyFactKey as F

    facts[F.LAST_DECLINE_WAS_HARD] = True
    result = evaluate(
        MINIMAL_RULE_SET, _debit(amount), facts, segment=Segment.B2C_STANDARD
    )
    assert result.decision.effect is PolicyEffect.DENY
    assert result.decision.deciding_rule_id == "POL-RAIL-001"


def test_an_open_hold_denies_a_debit():
    amount = Money.from_rupees(1499)
    facts = _clean_debit_facts(amount)
    from reclaim.contracts.policy_format import PolicyFactKey as F

    facts[F.HAS_OPEN_DISPUTE] = True
    result = evaluate(
        MINIMAL_RULE_SET, _debit(amount), facts, segment=Segment.B2C_STANDARD
    )
    assert result.decision.effect is PolicyEffect.DENY
    assert result.decision.deciding_rule_id == "POL-HOLDS-001"


# --------------------------------------------------------- scope of the set


def test_the_timing_gate_never_reaches_a_debit():
    """A mandate-authorised debit is not contact (CONTRACTS.md N6). The quiet-hours
    gate must not gate money movement -- that would be a different product."""
    from reclaim.policy.engine import applicable_rules

    applicable = applicable_rules(
        MINIMAL_RULE_SET,
        ActionType.SCHEDULE_DEBIT,
        segment=Segment.B2C_STANDARD,
        channel=None,
    )
    assert PolicyCategory.TIMING not in {r.category for r in applicable}
    assert PolicyCategory.TIMING in {
        r.category
        for r in applicable_rules(
            MINIMAL_RULE_SET,
            ActionType.SEND_MESSAGE,
            segment=Segment.B2C_STANDARD,
            channel=None,
        )
    }


def test_an_ungoverned_action_type_fails_closed():
    """``offer_payment_plan`` declares financial-authority, holds and integrity --
    all three of which this set gates. It still must not be allowed: the finance
    gate tests ``debit_amount``, which a payment plan's fact bundle does not carry,
    so the gate is unevaluable and its category is silent."""
    assert ActionType.OFFER_PAYMENT_PLAN not in GOVERNED_ACTION_TYPES
    plan = ActionEnvelope(
        action_id="act_1",
        case_id="case_1",
        action=OfferPaymentPlan(
            obligation_id="obl_1",
            total_amount=Money.from_rupees(2000),
            instalments=(
                PaymentPlanInstalment(
                    amount=Money.from_rupees(1000), due_at=TS + timedelta(days=7)
                ),
                PaymentPlanInstalment(
                    amount=Money.from_rupees(1000), due_at=TS + timedelta(days=14)
                ),
            ),
        ),
        proposed_by=PlanOrigin.DETERMINISTIC_ROUTER,
    )
    facts = _clean_debit_facts(Money.from_rupees(2000))
    from reclaim.contracts.policy_format import PolicyFactKey as F

    del facts[F.DEBIT_AMOUNT]  # not a fact about a payment plan
    result = evaluate(MINIMAL_RULE_SET, plan, facts, segment=Segment.B2C_STANDARD)
    assert result.decision.failed_closed is True
    assert PolicyCategory.FINANCIAL_AUTHORITY in result.silent_categories


def test_the_unimplemented_clauses_are_written_down():
    """§14.1 has clauses this set does not encode. They are recorded as data so
    "we implement §14.1" is never claimed wholesale."""
    assert rules_module.NOT_YET_ENCODED
    for clause, reason in rules_module.NOT_YET_ENCODED.items():
        assert clause.strip() and reason.strip()
