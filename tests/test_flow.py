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
from reclaim.contracts.enums import (
    ARM_SPECS,
    Arm,
    AutonomyTier,
    CaseState,
    PolicyEffect,
    RootCauseClass,
    StopReason,
)
from reclaim.contracts.money import Money
from reclaim.contracts.policy_format import PolicyThresholds
from reclaim.spine import audit_store, ledger, seed
from reclaim.spine.tables import outbox as outbox_table
from reclaim import flow


def _run(conn, **over):
    cases = seed.generate(conn, n=over.pop("n", 30))
    return cases, flow.run(conn, cases, **over)


def _by_case(results):
    return {r.case_id: r for r in results}


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


def test_a_contested_diagnosis_is_routed_to_a_human_not_acted_on(conn):
    """The deterministic diagnostician reports a contested dispatch below the
    policy confidence floor precisely so nothing downstream auto-acts on a coin
    flip between opposite interventions."""
    _, results = _run(conn)
    floor = Decimal(PolicyThresholds().diagnosis_confidence_floor)
    contested = [
        r for r in results if r.confidence is not None and r.confidence < floor
    ]
    assert contested
    for result in contested:
        assert result.outcome is flow.Outcome.ROUTED_TO_HUMAN_LOW_CONFIDENCE
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
