"""Phase 1: the whole path, on the seeded ledger.

Seed -> ledger -> deterministic diagnosis -> policy verdict -> (on allow) outbox
-> case state -> audit chain. Every test here runs against
``reclaim.spine.seed.generate``'s real output rather than a hand-built case: the
point is that the pieces fit around *the data the generator actually produces*,
including the arms it assigns and the amounts it draws.

What is real and what is a stand-in
-----------------------------------
Real: the obligations and cases, their amounts, segments, arms, decline classes
and detection times; the diagnosis; the rule evaluation; the outbox; the state
machine; the audit chain. Stand-in: the consent profiles and holds, because the
spine has no consent or holds store yet -- ``flow.stand_in_consent_profile`` and
``flow.stand_in_holds`` derive them deterministically from the payer id and are
named so that nothing about them reads as production.

The properties worth breaking the build over:

* a DENY writes **nothing** to the outbox, and the case ends stopped with the
  reason recorded;
* an ALLOW writes exactly one outbox row, and the case ends scheduled;
* every decision -- allow and deny alike -- leaves a ``policy_evaluated`` row
  carrying the rule ids (§14.1: every verdict is logged, including allows);
* the audit chain still verifies after the whole run;
* two runs of the same seed produce the same decisions. A flow that reads the
  wall clock, or hashes with the builtin ``hash()``, fails this one.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from reclaim.contracts.actions import ActionType
from reclaim.contracts.audit import verify_chain
from reclaim.contracts.case import Diagnosis
from reclaim.contracts.decline_taxonomy import DeclineClass
from reclaim.contracts.enums import (
    ARM_SPECS,
    Arm,
    AutonomyTier,
    CaseState,
    MessageIntent,
    ObligationKind,
    PolicyEffect,
    RiskClass,
    RootCauseClass,
    StopReason,
)
from reclaim.contracts.money import Money
from reclaim.contracts.obligations import Obligation, ObligationStatus
from reclaim.contracts.policy_format import PolicyThresholds
from reclaim.diagnosis.deterministic import diagnose
from reclaim.spine import audit_store, ledger, seed
from reclaim.spine.tables import outbox as outbox_table
from reclaim import flow


def _run(conn, **over):
    cases = seed.generate(conn, n=over.pop("n", 30))
    return cases, flow.run(conn, cases, **over)


def _by_case(results):
    return {r.case_id: r for r in results}


def _obligation_for(case):
    """The obligation a hand-built case refers to. Never persisted: the tests
    that use it call ``flow.hedged_route``, which is a pure function and touches
    only ``due_at`` (one template slot)."""
    return Obligation(
        obligation_id=case.obligation_id,
        kind=ObligationKind.SUBSCRIPTION_INVOICE,
        payer_id=case.payer_id,
        gross_amount=case.amount_at_risk,
        issued_at=case.detected_at - timedelta(days=30),
        due_at=case.detected_at - timedelta(days=2),
        status=ObligationStatus.OPEN,
    )


# ------------------------------------------------------------ the whole path


def test_the_run_produces_one_result_per_seeded_case(conn):
    cases, results = _run(conn)
    assert len(results) == len(cases)
    assert {r.case_id for r in results} == {c.case_id for c in cases}


def test_the_run_reaches_both_an_allow_and_a_deny_on_real_seed_data(conn):
    """If the seeded data only ever produced one verdict the flow would be
    untested by its own demo."""
    _, results = _run(conn)
    effects = {r.effect for r in results if r.effect is not None}
    assert PolicyEffect.ALLOW in effects
    assert PolicyEffect.DENY in effects


def test_an_allow_enqueues_exactly_one_action_and_schedules_the_case(conn):
    _, results = _run(conn)
    allowed = [r for r in results if r.outcome is flow.Outcome.ALLOWED]
    assert allowed, "no case was allowed; the flow would be proving nothing"
    for result in allowed:
        assert result.outbox_id is not None
        assert result.final_state is CaseState.SCHEDULED
        assert ledger.get_case(conn, result.case_id).state is CaseState.SCHEDULED

    enqueued = conn.execute(
        sa.select(outbox_table.c.case_id, outbox_table.c.action_type)
    ).fetchall()
    assert len(enqueued) == len(allowed)
    assert {row[0] for row in enqueued} == {r.case_id for r in allowed}


def test_a_deny_writes_nothing_to_the_outbox_and_stops_the_case(conn):
    _, results = _run(conn)
    denied = [r for r in results if r.outcome is flow.Outcome.DENIED]
    assert denied, "no case was denied; the deny path would be untested"
    enqueued_cases = {
        row[0] for row in conn.execute(sa.select(outbox_table.c.case_id)).fetchall()
    }
    for result in denied:
        assert result.outbox_id is None
        assert result.case_id not in enqueued_cases
        stopped = ledger.get_case(conn, result.case_id)
        assert stopped.state is CaseState.STOPPED
        assert stopped.stop_reason is StopReason.POLICY_BLOCKED


def test_every_deny_names_the_rule_that_decided_it(conn):
    """§14.1's verdict shape is ``DENY(rule_id, human_reason)``. A denial nobody
    can attribute is a denial nobody can appeal."""
    _, results = _run(conn)
    for result in results:
        if result.effect is not PolicyEffect.DENY:
            continue
        assert result.deciding_rule_id is not None
        assert result.reason.strip()


# ------------------------------------------------------------------- audit


def test_every_policy_decision_is_audited_including_the_allows(conn):
    _, results = _run(conn)
    rows = audit_store.read_all(conn)
    evaluated = [r for r in rows if r.event_type == "policy_evaluated"]
    decided = [r for r in results if r.effect is not None]
    assert len(evaluated) == len(decided)
    for row in evaluated:
        assert row.policy_decision is not None
        assert row.policy_verdict_rule_ids
        assert row.policy_version == flow.DEFAULT_RULE_SET.policy_version


def test_an_allowed_action_is_audited_with_its_idempotency_key(conn):
    _, results = _run(conn)
    scheduled = {
        row.case_id: row
        for row in audit_store.read_all(conn)
        if row.event_type == "action_scheduled"
    }
    allowed = [r for r in results if r.outcome is flow.Outcome.ALLOWED]
    assert set(scheduled) == {r.case_id for r in allowed}
    for row in scheduled.values():
        assert row.tool_call is not None
        assert row.idempotency_key == row.tool_call.idempotency_key


def test_the_audit_chain_verifies_after_the_whole_run(conn):
    _run(conn)
    report = verify_chain(audit_store.read_all(conn))
    assert report.is_valid, report.reason


# ------------------------------------------------------------ determinism


def test_two_runs_of_the_same_seed_decide_identically(engine):
    def once():
        with engine.begin() as conn:
            _, results = _run(conn)
            snapshot = [
                (r.case_id, r.outcome, r.effect, r.deciding_rule_id) for r in results
            ]
            conn.rollback()
        return snapshot

    assert once() == once()


def test_the_flow_never_reads_the_wall_clock_for_a_decision(conn):
    """Every evaluation instant is derived from the case's own detection time.

    A run that consulted ``utcnow()`` would decide quiet hours differently
    depending on when the suite happened to run -- green in the afternoon, red
    overnight, and irreproducible for a judge either way.
    """
    cases, results = _run(conn)
    detected = {c.case_id: c.detected_at for c in cases}
    for result in results:
        assert result.evaluated_at == detected[result.case_id] + flow.PLANNING_LATENCY


# ----------------------------------------------------- arms and abstention


def test_the_control_arm_takes_no_action_and_holds_no_plan(conn):
    """A0 is §12.2's natural-recovery floor. A plan on a control case would
    contaminate the control and overstate every other arm's lift."""
    _, results = _run(conn)
    control = [r for r in results if r.arm is Arm.A0]
    assert control, "the seeded run drew no control-arm case"
    for result in control:
        assert result.outcome is flow.Outcome.CONTROL_ARM_NO_ACTION
        assert result.effect is None
        case = ledger.get_case(conn, result.case_id)
        assert case.state is CaseState.DETECTED
        assert case.active_plan_id is None


def test_the_policy_disabled_arm_is_refused_rather_than_evaluated(conn):
    """JC-08: A5 runs with the policy engine off and is simulation-only. Deciding
    for it here would be the one place a real rail could be reached without a
    policy verdict."""
    _, results = _run(conn)
    disabled = [r for r in results if r.arm is Arm.A5]
    assert disabled
    assert not ARM_SPECS[Arm.A5].policy_engine_enabled
    for result in disabled:
        assert result.outcome is flow.Outcome.SIMULATION_ONLY_ARM_SKIPPED
        assert result.outbox_id is None


def test_a_contested_diagnosis_is_never_acted_on_as_though_it_were_clean(conn):
    """The deterministic diagnostician reports a contested dispatch below the
    policy confidence floor precisely so nothing downstream auto-acts on a coin
    flip between opposite interventions.

    This used to assert that such a case escalated and stopped. It no longer
    does -- ``flow.hedged_route`` lets it send one non-committal contact -- so
    what is pinned here is the part that did not change and must not: below the
    floor the case is either escalated with no action at all, or contacted as a
    declared hedge. There is no third path, and in particular no case arrives at
    the router's targeted verb by being unsure."""
    _, results = _run(conn)
    floor = Decimal(PolicyThresholds().diagnosis_confidence_floor)
    contested = [
        r for r in results if r.confidence is not None and r.confidence < floor
    ]
    assert contested
    for result in contested:
        if result.hedged:
            assert result.action_type is ActionType.SEND_MESSAGE
            continue
        assert result.outcome is flow.Outcome.ROUTED_TO_HUMAN_LOW_CONFIDENCE
        assert result.action_type is None
        assert ledger.get_case(conn, result.case_id).state is CaseState.ESCALATED


def test_lowering_the_confidence_floor_lets_a_contested_case_be_routed(conn):
    """The router's entries for the contested root causes are reachable -- they
    are gated by configuration, not dead."""
    permissive = PolicyThresholds(diagnosis_confidence_floor="0.500000")
    _, results = _run(conn, thresholds=permissive)
    routed = [
        r
        for r in results
        if r.root_cause is RootCauseClass.H4_AFA_STEP_UP_INCOMPLETE
        and r.effect is not None
    ]
    assert routed, "no contested H4 case reached the policy engine"


# ------------------------------------------------------ the deny reasons


def test_the_denials_are_not_all_the_same_rule(conn):
    """A run where every denial came from one gate would mean the other gates
    were never exercised by real data, whatever their unit tests say.

    Run at the coverage size: since POL-FIN-001 tiers up rather than denying, the
    default n=30 seed no longer reaches two *denying* gates."""
    _, results = _run(conn, n=COVERAGE_CASE_COUNT)
    deciding = {r.deciding_rule_id for r in results if r.effect is PolicyEffect.DENY}
    assert len(deciding) >= 2, deciding


def test_a_debit_above_the_afa_threshold_is_queued_for_approval_on_seed_data(conn):
    """§14.2 T2 / §9.1: a recurring debit above the AFA threshold is not stopped,
    it parks in AWAITING_APPROVAL with nothing enqueued, waiting for a human."""
    _, results = _run(conn)
    ceiling = PolicyThresholds().afa_required_above
    big_debits = [
        r
        for r in results
        if r.action_type is ActionType.SCHEDULE_DEBIT and r.amount_at_risk > ceiling
    ]
    assert big_debits
    awaiting = {c.case_id for c in ledger.list_awaiting_approval(conn)}
    for result in big_debits:
        assert result.effect is PolicyEffect.ALLOW_WITH_APPROVAL
        assert result.deciding_rule_id == "POL-FIN-001"
        assert result.requires_tier is AutonomyTier.T2
        assert result.final_state is CaseState.AWAITING_APPROVAL
        assert result.outbox_id is None
        assert result.case_id in awaiting


# --------------------------------------------------------------- stand-ins


def test_the_stand_in_consent_is_deterministic_and_varied(conn):
    """It is a stand-in, but it must not be a constant: a consent gate that
    always allows is a consent gate that has never been exercised."""
    profiles = [flow.stand_in_consent_profile(f"payer_{i:04d}") for i in range(1, 31)]
    assert profiles[0] == flow.stand_in_consent_profile("payer_0001")
    assert any(p.on_dnc_list for p in profiles)
    assert any(
        any(not record.is_effective for record in p.records) for p in profiles
    )
    assert any(p.quiet_hours is not None for p in profiles)
    assert len({p.quiet_hours.timezone_name for p in profiles if p.quiet_hours}) >= 2


def test_the_router_only_emits_governed_verbs(conn):
    from reclaim.policy.rules import GOVERNED_ACTION_TYPES

    _, results = _run(conn)
    emitted = {r.action_type for r in results if r.action_type is not None}
    assert emitted <= set(GOVERNED_ACTION_TYPES)
    assert emitted == set(GOVERNED_ACTION_TYPES), (
        "both governed verbs should appear on seeded data, or one path is untested"
    )


# --------------------------------------------------- coverage on real data

#: Large enough that the arm draw, the decline-class draw and the amount draw
#: each produce their whole range. At the generator's default of 30 the flow
#: decides only six cases and never *allows* a contact, so the contact-allow path
#: would be exercised by unit tests alone -- which is the exact gap CONTRACTS.md
#: §6 is a list of.
COVERAGE_CASE_COUNT = 200


def test_a_large_seeded_run_exercises_more_than_one_gate_and_both_verbs(conn):
    _, results = _run(conn, n=COVERAGE_CASE_COUNT)
    decided = [r for r in results if r.effect is not None]

    denying = {
        r.deciding_rule_id for r in decided if r.effect is PolicyEffect.DENY
    }
    assert len(denying) >= 3, denying

    allowed_verbs = {
        r.action_type for r in decided if r.effect is PolicyEffect.ALLOW
    }
    assert allowed_verbs == {ActionType.SCHEDULE_DEBIT, ActionType.SEND_MESSAGE}, (
        "a contact that policy allows is the path the whole product exists for; "
        "if no seeded case reaches it, only the unit tests have"
    )

    # POL-FIN-001's coverage moved from DENY to ALLOW_WITH_APPROVAL: a high-value
    # debit still has to reach the finance gate on real data, it just parks now.
    approving = [
        r for r in decided if r.effect is PolicyEffect.ALLOW_WITH_APPROVAL
    ]
    assert approving, "no seeded debit reached the AFA-threshold approval path"
    for result in approving:
        assert result.deciding_rule_id == "POL-FIN-001"
        assert result.action_type is ActionType.SCHEDULE_DEBIT
        assert result.final_state is CaseState.AWAITING_APPROVAL


def test_an_allow_with_approval_parks_the_case_and_enqueues_nothing(conn):
    """The core of the change: an ALLOW_WITH_APPROVAL leaves the case in
    AWAITING_APPROVAL, writes no outbox row, and shows up on the pending list --
    money never waits on a human from inside the executor."""
    _, results = _run(conn, n=COVERAGE_CASE_COUNT)
    pending = [r for r in results if r.outcome is flow.Outcome.PENDING_APPROVAL]
    assert pending, "no seeded case reached the approval path"

    enqueued_cases = {
        row[0] for row in conn.execute(sa.select(outbox_table.c.case_id)).fetchall()
    }
    for result in pending:
        assert result.effect is PolicyEffect.ALLOW_WITH_APPROVAL
        assert result.requires_tier is AutonomyTier.T2
        assert result.final_state is CaseState.AWAITING_APPROVAL
        assert result.outbox_id is None
        assert result.case_id not in enqueued_cases
        case = ledger.get_case(conn, result.case_id)
        assert case.state is CaseState.AWAITING_APPROVAL
        assert case.active_diagnosis_id is not None

    assert {c.case_id for c in ledger.list_awaiting_approval(conn)} == {
        r.case_id for r in pending
    }


def test_the_approval_request_is_audited_with_the_decision_and_tier(conn):
    """§14.1: every verdict is logged. An ALLOW_WITH_APPROVAL also leaves an
    ``approval_requested`` row so the human queue has the decision and its tier."""
    _, results = _run(conn, n=COVERAGE_CASE_COUNT)
    pending = [r for r in results if r.outcome is flow.Outcome.PENDING_APPROVAL]
    assert pending

    rows = {
        row.case_id: row
        for row in audit_store.read_all(conn)
        if row.event_type == "approval_requested"
    }
    assert set(rows) == {r.case_id for r in pending}
    for row in rows.values():
        assert row.policy_decision is not None
        assert row.policy_decision.effect is PolicyEffect.ALLOW_WITH_APPROVAL
        assert row.policy_decision.requires_tier is AutonomyTier.T2
        assert row.policy_version == flow.DEFAULT_RULE_SET.policy_version


def test_the_consent_and_holds_gates_deny_on_real_seeded_data(conn):
    """Both gates read a stand-in store, so this asserts the stand-in is varied
    enough to reach them -- not that the store is real."""
    _, results = _run(conn, n=COVERAGE_CASE_COUNT)
    deciding = {r.deciding_rule_id for r in results if r.effect is PolicyEffect.DENY}
    assert "POL-CONSENT-001" in deciding
    assert "POL-HOLDS-001" in deciding


# ------------------------------------------ the contested-diagnosis fallback
#
# Below the confidence floor the case used to stop dead: escalated to a human
# queue that Phase 1 has no consumer for. These tests pin the narrower rule that
# replaced it -- a *contested* dispatch (a named primary plus named alternatives)
# earns the least-committal contact that is true under every named hypothesis,
# and nothing else changes.


def _contested(results):
    floor = Decimal(PolicyThresholds().diagnosis_confidence_floor)
    return [r for r in results if r.confidence is not None and r.confidence < floor]


def test_a_contested_diagnosis_takes_a_hedged_contact_rather_than_stopping_dead(conn):
    """The behaviour change. A contested dispatch reaches the policy engine on a
    hedged contact instead of escalating to a queue with no consumer."""
    _, results = _run(conn, n=COVERAGE_CASE_COUNT)
    contested = _contested(results)
    assert contested
    hedged = [r for r in contested if r.hedged]
    assert hedged, "no contested case took the hedged contact"
    for result in hedged:
        assert result.effect is not None, "the hedge skipped the policy engine"
        assert result.action_type is ActionType.SEND_MESSAGE


def test_a_contested_diagnosis_never_schedules_a_debit(conn):
    """The load-bearing half. §9.2 H3: retrying a dead mandate is 0% and still
    costs a fee, and a contested dispatch is exactly the state where we cannot
    rule that out. A hedge is a contact or it is nothing."""
    _, results = _run(conn, n=COVERAGE_CASE_COUNT)
    for result in _contested(results):
        assert result.action_type is not ActionType.SCHEDULE_DEBIT


def test_the_hedge_says_only_what_is_true_of_the_case(conn):
    """The hedge intent is chosen by what actually happened, not by convenience.

    ``PAYMENT_FAILED_INFORM`` on an overdue receivable would be a false statement
    -- no debit was ever attempted on it -- and a registered template that lies is
    worse than an escalation. So a failed debit gets the failure notice and a
    receivable gets a reminder."""
    _, results = _run(conn, n=COVERAGE_CASE_COUNT)
    hedged = [r for r in _contested(results) if r.hedged]
    assert hedged
    seen = set()
    for result in hedged:
        expected = (
            MessageIntent.PAYMENT_FAILED_INFORM
            if result.decline_class is not None
            else MessageIntent.PAYMENT_REMINDER
        )
        assert result.hedged_intent is expected
        seen.add(expected)
    assert seen == {
        MessageIntent.PAYMENT_FAILED_INFORM,
        MessageIntent.PAYMENT_REMINDER,
    }, "the seed no longer exercises both hedge branches"


def test_the_receivable_hedge_is_the_same_verb_h9_would_have_routed(conn):
    """A disclosed weakness, pinned so it cannot be forgotten.

    For a D3 overdue receivable the hedge is ``PAYMENT_REMINDER`` -- exactly what
    the router picks for H9 above the floor. So on this branch the fallback is
    equivalent to conceding that filing H8 as an alternative never made the
    dispatch genuinely contested: H9 (chase) and H8 (our invoice is wrong) do not
    demand opposite contacts. The honest fix is in the diagnostician's rung
    selection. If someone repairs that, this test fails and points at the note in
    ``flow.hedged_route`` explaining why."""
    assert (
        flow.HEDGED_RECEIVABLE_INTENT
        is flow._ROUTES[RootCauseClass.H9_B2B_LIQUIDITY_OR_WILLFUL_DELAY]
    )


def test_an_abstaining_diagnosis_is_refused_a_hedge(make_case):
    """``root_cause=UNKNOWN`` is not contested, it is blank. There is no
    hypothesis for a hedge to be true under, so nothing is sent.

    Tested against ``hedged_route`` directly rather than through the seed,
    because ``seed.generate`` draws only four decline classes and none of them
    abstains -- so a seeded assertion here would pass over an empty list. That
    gap is itself worth knowing: the H6 abstention (§10.1's most damaging wrong
    call) is the branch this rule most needs to refuse, and the seeded run never
    reaches it."""
    case = make_case(canonical_decline_class=DeclineClass.PROCESSING_ERROR)
    obligation = _obligation_for(case)
    dx = diagnose(case, created_at=case.detected_at)
    assert dx.root_cause is RootCauseClass.UNKNOWN
    assert flow.hedged_route(case, obligation, dx, at=case.detected_at) is None


def test_a_receivable_hedge_is_refused_when_a_candidate_forbids_the_nudge(
    make_case,
):
    """The only truthful contact for a receivable is a reminder, and a reminder is
    a payment nudge. §9.2 forbids one under H5/H6/H7, so on that combination there
    is nothing honest left to send and the case escalates."""
    case = make_case(risk_class=RiskClass.OVERDUE_RECEIVABLE)
    obligation = _obligation_for(case)
    dx = diagnose(case, created_at=case.detected_at)
    assert dx.root_cause is RootCauseClass.H9_B2B_LIQUIDITY_OR_WILLFUL_DELAY
    assert flow.hedged_route(case, obligation, dx, at=case.detected_at) is not None

    # Rebuilt field by field, not via ``model_dump``: dumping a Diagnosis emits
    # its computed fields too, and feeding those back to an ``extra="forbid"``
    # model is a validation error rather than a round trip. ``model_copy`` is not
    # an option either -- it skips the validators this test needs to run.
    disputed = Diagnosis(
        **{name: getattr(dx, name) for name in Diagnosis.model_fields},
    ).model_copy(
        update={"alternative_root_causes": (RootCauseClass.H7_COMMERCIAL_DISPUTE,)}
    )
    assert disputed.alternative_root_causes == (
        RootCauseClass.H7_COMMERCIAL_DISPUTE,
    )
    assert flow.hedged_route(case, obligation, disputed, at=case.detected_at) is None


def test_the_hedged_contact_is_recorded_as_contested_in_the_audit_chain(conn):
    """A hedge must be auditable as a hedge. Reading back a scheduled contact and
    finding no trace that its diagnosis was contested would let the run be
    reported as a confident intervention."""
    _, results = _run(conn, n=COVERAGE_CASE_COUNT)
    hedged = {r.case_id for r in results if r.hedged}
    assert hedged
    rows = {
        row.case_id
        for row in audit_store.read_all(conn)
        if row.event_type == "contested_diagnosis_hedged"
    }
    assert rows == hedged


def test_the_hedge_leaves_the_confidence_untouched(conn):
    """The diagnosis did not get better. Only its consequence changed."""
    _, results = _run(conn, n=COVERAGE_CASE_COUNT)
    floor = Decimal(PolicyThresholds().diagnosis_confidence_floor)
    for result in results:
        if result.hedged:
            assert result.confidence < floor
