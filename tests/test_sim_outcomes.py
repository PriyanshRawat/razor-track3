"""The simulated PSP response layer: arms A0, A1 and A4 (§12.2, cut per §18.4).

What these tests are guarding is not "the numbers are right" -- they are assumptions
(``anchors.ANCHOR_HONESTY_NOTE``) and no test can check them. They guard the four
things that decide whether the resulting scoreboard is *honest*:

1. **Each arm gets only its own mechanism.** A0 is resolved from natural recovery
   alone and is never given a plan, a contact or an outbox row -- contaminate the
   control and every arm's lift is overstated. A1's uplift does not read the decline
   code. A4's uplift is earned by choosing the verb §9.2 prescribes, so a debit retry
   against a dead mandate recovers nothing (§9.2 H3).
2. **Nothing is auto-approved.** A case parked in ``AWAITING_APPROVAL`` by the policy
   engine stays parked. A simulator that resolved it would be quietly granting the
   T2 approval §14.2 requires a human for.
3. **The draw is reproducible across processes.** ``hashlib``, not the builtin
   ``hash()`` (invariant #5): a ``PYTHONHASHSEED``-dependent draw passes every
   in-process test and silently re-randomises the judge's reproduction.
4. **The audit trail says "simulated" in those words.** Every resolved case leaves
   one row that is unmistakably not a real rail, carries no ``tool_call`` and no
   idempotency key, and records the probability and the draw that produced it.

Plus §12.5.4 item 4, which is the reason this package exists separately at all: a
test asserts no agent code path imports the simulator.
"""

from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from reclaim.contracts.actions import ActionType
from reclaim.contracts.audit import verify_chain
from reclaim.contracts.decline_taxonomy import DeclineClass
from reclaim.contracts.enums import Arm, CaseState, StopReason
from reclaim.contracts.money import Money
from reclaim.contracts.units import PROBABILITY_SCALE
from reclaim.sim import anchors, outcomes
from reclaim.spine import audit_store, case_machine, ledger, outbox, seed
from reclaim.spine.tables import outbox as outbox_table
from reclaim import flow

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------- helpers


def _case_id_with_draw(predicate, arm: Arm) -> str:
    """A seeded-looking case id whose draw satisfies ``predicate``.

    The draw is a pure function of ``(salt, case_id, arm)``, so a test that needs a
    *certain* recovery can pick an id rather than mock the randomness away.
    """
    for n in range(1, 600):
        case_id = f"case_{n:04d}"
        if predicate(outcomes.draw_for(case_id, arm)):
            return case_id
    raise AssertionError("no case id in range produced the required draw")


def _open(conn, make_obligation, make_case, **over):
    """Open one case (and its obligation) in the ledger, returning the case."""
    obligation_id = over.pop("obligation_id", f"obl_{over['case_id'].split('_')[-1]}")
    ledger.upsert_obligation(
        conn,
        make_obligation(
            obligation_id=obligation_id,
            gross_amount=over.get("amount_at_risk", Money.from_rupees(1499)),
        ),
    )
    case = make_case(obligation_id=obligation_id, **over)
    return ledger.open_case(conn, case)


def _schedule(conn, case, envelope):
    """Walk a case to ``SCHEDULED`` with ``envelope`` enqueued, as ``flow._allow``
    would. Used for the counterfactual actions the real router never proposes."""
    outbox.enqueue(conn, envelope, at=case.detected_at)
    case_machine.transition(conn, case.case_id, CaseState.DIAGNOSING)
    case_machine.transition(conn, case.case_id, CaseState.PLANNED)
    return case_machine.transition(conn, case.case_id, CaseState.SCHEDULED)


def _sim_rows(conn, case_id=None):
    rows = [
        r
        for r in audit_store.read_all(conn)
        if r.event_type == outcomes.SIMULATED_RESPONSE_EVENT
    ]
    return [r for r in rows if case_id is None or r.case_id == case_id]


# ------------------------------------------------------- A0: the control arm


def test_the_control_arm_is_resolved_from_natural_recovery_alone(
    conn, make_obligation, make_case
):
    case = _open(
        conn,
        make_obligation,
        make_case,
        case_id="case_0001",
        arm=Arm.A0,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    result = outcomes.resolve_case(conn, case.case_id)
    assert result.lane is outcomes.SimLane.NATURAL_ONLY
    assert result.probability == anchors.natural_probability(
        DeclineClass.INSUFFICIENT_FUNDS, None
    )
    assert result.action_type is None
    assert result.simulated_contacts == 0


def test_a_recovered_control_case_is_stopped_as_already_paid(
    conn, make_obligation, make_case
):
    """§9.1 has **no** edge from ``DETECTED`` to ``RECOVERED``: every path into
    ``RECOVERED`` runs through ``EXECUTING`` or a reconciliation, both of which mean
    somebody acted. A control case that self-heals is therefore recorded as
    ``STOPPED(already_paid)`` -- the money arrived and we did nothing. This test
    exists to pin that compromise in place: the recovered *amount* has to be read
    off the outcome, never off ``state is RECOVERED``, or A0 silently scores zero
    and every lift measured against it is inflated.
    """
    case_id = _case_id_with_draw(lambda d: d < Decimal("0.02"), Arm.A0)
    case = _open(
        conn, make_obligation, make_case, case_id=case_id, arm=Arm.A0,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    result = outcomes.resolve_case(conn, case.case_id)
    assert result.recovered is True
    assert result.recovered_amount == case.amount_at_risk
    assert result.final_state is CaseState.STOPPED
    stored = ledger.get_case(conn, case.case_id)
    assert stored.state is CaseState.STOPPED
    assert stored.stop_reason is StopReason.ALREADY_PAID


def test_a_control_case_that_does_not_recover_is_left_exactly_where_it_was(
    conn, make_obligation, make_case
):
    case_id = _case_id_with_draw(lambda d: d > Decimal("0.98"), Arm.A0)
    case = _open(
        conn, make_obligation, make_case, case_id=case_id, arm=Arm.A0,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    result = outcomes.resolve_case(conn, case.case_id)
    assert result.recovered is False
    assert result.recovered_amount == Money.zero()
    assert ledger.get_case(conn, case.case_id).state is CaseState.DETECTED


def test_the_control_arm_is_never_given_a_plan_or_a_contact_or_an_outbox_row(
    conn, make_obligation, make_case
):
    """§12.2's whole point: A0 takes no action. ``RiskCase`` already refuses a plan on
    A0; the simulator must not smuggle one in through a contact either."""
    for n in (1, 2, 3, 4, 5):
        _open(
            conn, make_obligation, make_case, case_id=f"case_{n:04d}", arm=Arm.A0,
            canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
        )
    resolution = outcomes.resolve_batch(
        conn, [ledger.get_case(conn, f"case_{n:04d}") for n in (1, 2, 3, 4, 5)]
    )
    assert all(o.simulated_contacts == 0 for o in resolution.outcomes)
    assert all(
        ledger.get_case(conn, o.case_id).active_plan_id is None
        for o in resolution.outcomes
    )
    assert conn.execute(
        sa.select(sa.func.count()).select_from(outbox_table)
    ).scalar_one() == 0


# --------------------------------------------------- A1: the naive baseline


def test_the_naive_baseline_uses_the_same_uplift_for_two_decline_reasons(
    conn, make_obligation, make_case
):
    """A1 is undifferentiated by construction. If its probability moved with the
    decline class it would be borrowing A3's mechanism."""
    pairs = {
        "case_0011": DeclineClass.INSUFFICIENT_FUNDS,
        "case_0012": DeclineClass.MANDATE_CANCELLED,
    }
    for case_id, decline_class in pairs.items():
        _open(
            conn, make_obligation, make_case, case_id=case_id, arm=Arm.A1,
            canonical_decline_class=decline_class,
        )
    deltas = set()
    for case_id, decline_class in pairs.items():
        result = outcomes.resolve_case(conn, case_id)
        assert result.lane is outcomes.SimLane.GENERIC_BASELINE
        deltas.add(result.probability - anchors.natural_probability(decline_class, None))
    assert deltas == {anchors.GENERIC_TOTAL_UPLIFT}


def test_a_recovered_baseline_case_reaches_recovered_through_the_state_machine(
    conn, make_obligation, make_case
):
    case_id = _case_id_with_draw(lambda d: d < Decimal("0.02"), Arm.A1)
    case = _open(
        conn, make_obligation, make_case, case_id=case_id, arm=Arm.A1,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    result = outcomes.resolve_case(conn, case.case_id)
    assert result.recovered is True
    assert result.final_state is CaseState.RECOVERED
    assert ledger.get_case(conn, case.case_id).state is CaseState.RECOVERED
    assert result.simulated_contacts == anchors.GENERIC_TOUCH_COUNT


def test_a_baseline_case_that_gets_no_reply_stops_at_the_contact_cap(
    conn, make_obligation, make_case
):
    """A static 4-touch drip that ran all four touches with no payment has exhausted
    its ladder -- §14.3's ``contact_cap``, not an open case waiting for a scheduler
    that does not exist."""
    case_id = _case_id_with_draw(lambda d: d > Decimal("0.98"), Arm.A1)
    case = _open(
        conn, make_obligation, make_case, case_id=case_id, arm=Arm.A1,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    result = outcomes.resolve_case(conn, case.case_id)
    assert result.recovered is False
    assert result.final_state is CaseState.STOPPED
    assert ledger.get_case(conn, case.case_id).stop_reason is StopReason.CONTACT_CAP


def test_the_simulated_baseline_action_never_reaches_the_outbox(
    conn, make_obligation, make_case
):
    """The outbox is exactly-once *execution*. A1's drip was never proposed by the
    router and never evaluated by the policy engine, so it must not appear there as
    though it had been."""
    case = _open(
        conn, make_obligation, make_case, case_id="case_0013", arm=Arm.A1,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    outcomes.resolve_case(conn, case.case_id)
    assert conn.execute(
        sa.select(sa.func.count()).select_from(outbox_table)
    ).scalar_one() == 0


# ------------------------------------------------------------ A4: the agent


def test_only_a_case_that_reached_scheduled_earns_the_targeted_uplift(
    conn, make_obligation, make_case
):
    """An A4 case the agent never acted on is still *in* the experiment, so it gets
    the natural floor -- but it must not get the TARGETED lane, because no action
    was taken to credit. The distinction between "resolved" and "acted on" is the
    whole reason the two lanes exist."""
    case = _open(
        conn, make_obligation, make_case, case_id="case_0021", arm=Arm.A4,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    still_detected = outcomes.resolve_case(conn, case.case_id)
    assert still_detected.lane is outcomes.SimLane.NATURAL_FLOOR
    assert still_detected.lane is not outcomes.SimLane.TARGETED_AGENT
    assert still_detected.action_type is None
    assert still_detected.probability == anchors.natural_probability(
        DeclineClass.INSUFFICIENT_FUNDS, None
    )


def test_a_parked_approval_case_is_never_auto_approved(
    conn, make_obligation, make_case, make_debit_envelope
):
    """§14.2: T2 means a human approves. The parked case now gets a natural-recovery
    floor (it is still in the experiment and natural recovery did not stop), but the
    property that matters is unchanged and is what this test pins: it must never
    reach ``SCHEDULED``. ``awaiting_approval -> scheduled`` is the *approve* edge,
    and a simulator taking it would be signing off on the human's behalf.
    """
    case_id = _case_id_with_draw(lambda d: d > Decimal("0.98"), Arm.A4)
    case = _open(
        conn, make_obligation, make_case, case_id=case_id, arm=Arm.A4,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    case_machine.transition(conn, case.case_id, CaseState.DIAGNOSING)
    case_machine.transition(conn, case.case_id, CaseState.PLANNED)
    case_machine.transition(conn, case.case_id, CaseState.AWAITING_APPROVAL)

    result = outcomes.resolve_case(conn, case.case_id)
    assert result.lane is outcomes.SimLane.NATURAL_FLOOR
    assert "approval" in result.reason
    assert result.action_type is None
    assert result.final_state is not CaseState.SCHEDULED
    # This draw does not recover, so the case is left exactly where the human left it.
    assert ledger.get_case(conn, case.case_id).state is CaseState.AWAITING_APPROVAL
    assert conn.execute(
        sa.select(sa.func.count()).select_from(outbox_table)
    ).scalar_one() == 0


def test_a_debit_retry_against_a_dead_mandate_recovers_nothing_and_stops_hard(
    conn, make_obligation, make_case, make_debit_envelope
):
    """§9.2 H3, end to end: the retry earns no uplift at all, and because the class
    needs a new mandate the attempt is terminal rather than backing off for another
    go. This is the counterfactual a retry engine would run."""
    case_id = _case_id_with_draw(lambda d: d > Decimal("0.20"), Arm.A4)
    case = _open(
        conn, make_obligation, make_case, case_id=case_id, arm=Arm.A4,
        canonical_decline_class=DeclineClass.MANDATE_CANCELLED,
    )
    _schedule(
        conn,
        case,
        make_debit_envelope(
            case_id=case.case_id, obligation_id=case.obligation_id, action_id="act_x"
        ),
    )
    result = outcomes.resolve_case(conn, case.case_id)
    assert result.lane is outcomes.SimLane.TARGETED_AGENT
    assert result.action_type is ActionType.SCHEDULE_DEBIT
    assert result.probability == anchors.natural_probability(
        DeclineClass.MANDATE_CANCELLED, None
    )
    assert result.recovered is False
    assert result.final_state is CaseState.STOPPED
    stored = ledger.get_case(conn, case.case_id)
    assert stored.stop_reason is StopReason.HARD_DECLINE_NO_FURTHER_DEBIT


def test_the_reauth_contact_outscores_the_retry_on_the_same_dead_mandate(
    conn, make_obligation, make_case
):
    """The whole A3/A4 claim in one assertion: same failure, two verbs, and the
    simulator pays only for the one §9.2 prescribes."""
    dead = DeclineClass.MANDATE_CANCELLED
    retry = anchors.targeted_probability(dead, None, ActionType.SCHEDULE_DEBIT)
    reauth = anchors.targeted_probability(dead, None, ActionType.SEND_MESSAGE)
    assert reauth > retry
    assert retry == anchors.natural_probability(dead, None)


def test_an_nsf_retimed_debit_is_ranked_above_the_baseline_and_the_control():
    nsf = DeclineClass.INSUFFICIENT_FUNDS
    natural = anchors.natural_probability(nsf, None)
    generic = anchors.generic_probability(nsf, None)
    targeted = anchors.targeted_probability(nsf, None, ActionType.SCHEDULE_DEBIT)
    assert natural < generic < targeted


def test_a_soft_declined_retry_backs_off_rather_than_stopping(
    conn, make_obligation, make_case, make_debit_envelope
):
    """An NSF retry that fails again is not a hard decline: another attempt is
    legitimate, there is just no scheduler to make it. ``RETRY_BACKOFF`` says that
    without pretending the case is finished."""
    case_id = _case_id_with_draw(lambda d: d > Decimal("0.98"), Arm.A4)
    case = _open(
        conn, make_obligation, make_case, case_id=case_id, arm=Arm.A4,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    _schedule(
        conn,
        case,
        make_debit_envelope(
            case_id=case.case_id, obligation_id=case.obligation_id, action_id="act_y"
        ),
    )
    result = outcomes.resolve_case(conn, case.case_id)
    assert result.recovered is False
    assert result.final_state is CaseState.RETRY_BACKOFF


def test_the_arms_cut_at_t_minus_12h_are_reported_as_not_simulated(
    conn, make_obligation, make_case
):
    """§18.4 cuts A2 and A5 and this pass also drops A3. Each of their cases comes
    back labelled, not silently given a number."""
    for n, arm in enumerate((Arm.A2, Arm.A3, Arm.A5), start=31):
        _open(
            conn, make_obligation, make_case, case_id=f"case_{n:04d}", arm=arm,
            canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
        )
        result = outcomes.resolve_case(conn, f"case_{n:04d}")
        assert result.lane is outcomes.SimLane.NOT_SIMULATED
        assert arm.value in result.reason
    assert set(outcomes.IN_SCOPE_ARMS) == {Arm.A0, Arm.A1, Arm.A4}


# ------------------------------------------------------------- the audit row


def test_every_resolved_case_writes_exactly_one_simulated_response_row(
    conn, make_obligation, make_case
):
    for n in (41, 42, 43):
        _open(
            conn, make_obligation, make_case, case_id=f"case_{n:04d}", arm=Arm.A1,
            canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
        )
        outcomes.resolve_case(conn, f"case_{n:04d}")
        assert len(_sim_rows(conn, f"case_{n:04d}")) == 1


def test_the_simulated_row_claims_no_tool_call_and_no_idempotency_key(
    conn, make_obligation, make_case
):
    """Invariant #8 ties a ``tool_call`` and an idempotency key to a real external
    action. Nothing external happened here, so claiming either would make a
    simulated response indistinguishable from a rail call in the audit log."""
    _open(
        conn, make_obligation, make_case, case_id="case_0044", arm=Arm.A1,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    outcomes.resolve_case(conn, "case_0044")
    row = _sim_rows(conn, "case_0044")[0]
    assert row.tool_call is None
    assert row.idempotency_key is None


def test_the_audit_rationale_says_the_response_is_simulated(
    conn, make_obligation, make_case
):
    _open(
        conn, make_obligation, make_case, case_id="case_0045", arm=Arm.A1,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    result = outcomes.resolve_case(conn, "case_0045")
    row = _sim_rows(conn, "case_0045")[0]
    assert "SIMULATED" in row.decision_rationale
    assert str(result.probability) in row.decision_rationale
    assert str(result.draw) in row.decision_rationale


def test_the_simulated_event_type_is_absent_from_the_frozen_vocabulary():
    """Deliberate: ``AUDIT_EVENT_TYPES`` is the list of *real* log points (JC-31,
    open and unenforced). Keeping the simulator's event out of it means a reviewer
    grepping that set can tell at a glance which rows never touched a rail."""
    from reclaim.contracts.audit import AUDIT_EVENT_TYPES

    assert outcomes.SIMULATED_RESPONSE_EVENT not in AUDIT_EVENT_TYPES


def test_the_audit_chain_still_verifies_after_a_batch_resolves(conn):
    cases = seed.generate(conn, n=30)
    flow.run(conn, cases)
    outcomes.resolve_batch(conn, cases)
    verification = verify_chain(audit_store.read_all(conn))
    assert verification.is_valid, verification.reason


# ----------------------------------------------------------- the draw itself


def test_the_draw_matches_its_documented_sha256_recipe():
    """Re-derived here from the docstring's recipe rather than read back from the
    module: a test that asks the implementation for its expectation cannot notice a
    silent re-randomisation of the whole book."""
    for case_id in ("case_0001", "case_0199", "case_2000"):
        for arm in (Arm.A0, Arm.A1, Arm.A4):
            payload = f"{outcomes.SIM_SALT}|{case_id}|{arm.value}".encode("utf-8")
            digest = hashlib.sha256(payload).digest()
            expected = Decimal(int.from_bytes(digest[:8], "big")) / Decimal(2**64)
            assert outcomes.draw_for(case_id, arm) == anchors.probability(expected)


def test_the_draw_is_uniform_enough_not_to_bias_an_arm():
    draws = [outcomes.draw_for(f"case_{n:05d}", Arm.A4) for n in range(4000)]
    below_half = sum(1 for d in draws if d < Decimal("0.5"))
    assert 1850 < below_half < 2150  # ~3 sigma on 4000 fair draws
    assert min(draws) < Decimal("0.01") and max(draws) > Decimal("0.99")


def test_the_draw_is_a_six_dp_decimal_so_it_can_be_hashed_and_replayed():
    draw = outcomes.draw_for("case_0007", Arm.A1)
    assert -draw.as_tuple().exponent == PROBABILITY_SCALE
    assert Decimal(0) <= draw < Decimal(1)


def test_the_draw_is_stable_across_processes():
    """Invariant #5. The builtin ``hash()`` passes every test above and fails this
    one, because ``PYTHONHASHSEED`` is randomised per process."""
    body = (
        "print([str(draw_for(f'case_{n:04d}', Arm.A4)) for n in range(60)])"
    )
    preamble = (
        "import sys; sys.path.insert(0, %r)\n"
        "from reclaim.contracts.enums import Arm\n"
        "from reclaim.sim.outcomes import draw_for\n"
    ) % str(_REPO_ROOT)
    seen = set()
    for hashseed in ("0", "1", "999"):
        env = dict(os.environ, PYTHONHASHSEED=hashseed, PYTHONIOENCODING="utf-8")
        done = subprocess.run(
            [sys.executable, "-c", preamble + body],
            capture_output=True, text=True, env=env, cwd=str(_REPO_ROOT),
        )
        assert done.returncode == 0, done.stderr
        seen.add(done.stdout.strip())
    assert len(seen) == 1


# ------------------------------------------------------------ the batch runner


def test_resolving_a_second_time_changes_nothing(conn):
    cases = seed.generate(conn, n=30)
    flow.run(conn, cases)
    first = outcomes.resolve_batch(conn, cases)
    states = {c.case_id: ledger.get_case(conn, c.case_id).state for c in cases}
    rows_before = len(audit_store.read_all(conn))

    second = outcomes.resolve_batch(conn, cases)
    assert all(o.lane is outcomes.SimLane.NOT_SIMULATED for o in second.outcomes)
    assert {c.case_id: ledger.get_case(conn, c.case_id).state for c in cases} == states
    assert len(audit_store.read_all(conn)) == rows_before
    assert first.by_arm[Arm.A0].gross_recovered.paise >= 0


def test_the_per_arm_tally_adds_up_to_the_outcomes_it_summarises(conn):
    cases = seed.generate(conn, n=200)
    flow.run(conn, cases)
    resolution = outcomes.resolve_batch(conn, cases)

    for arm, tally in resolution.by_arm.items():
        mine = [o for o in resolution.outcomes if o.arm is arm]
        assert tally.case_count == len(mine)
        assert tally.recovered_case_count == sum(1 for o in mine if o.recovered)
        assert tally.gross_recovered.paise == sum(
            o.recovered_amount.paise for o in mine
        )
        assert tally.total_at_risk.paise == sum(o.amount_at_risk.paise for o in mine)
        assert tally.gross_recovered <= tally.total_at_risk  # invariant #6


def test_a_seeded_batch_resolves_all_three_arms_that_are_in_scope(conn):
    cases = seed.generate(conn, n=200)
    flow.run(conn, cases)
    resolution = outcomes.resolve_batch(conn, cases)
    lanes = {o.lane for o in resolution.outcomes}
    assert outcomes.SimLane.NATURAL_ONLY in lanes
    assert outcomes.SimLane.GENERIC_BASELINE in lanes
    assert outcomes.SimLane.TARGETED_AGENT in lanes
    assert outcomes.SimLane.NOT_SIMULATED in lanes


def test_no_recovered_amount_exceeds_the_amount_at_risk(conn):
    cases = seed.generate(conn, n=200)
    flow.run(conn, cases)
    for outcome in outcomes.resolve_batch(conn, cases).outcomes:
        assert outcome.recovered_amount <= outcome.amount_at_risk
        if not outcome.recovered:
            assert outcome.recovered_amount == Money.zero()


def test_a_batch_resolves_the_same_way_twice_from_the_same_seed(engine):
    def once():
        with engine.begin() as c:
            cases = seed.generate(c, n=30)
            flow.run(c, cases)
            resolution = outcomes.resolve_batch(c, cases)
            return [(o.case_id, o.lane.value, o.recovered) for o in resolution.outcomes]

    first = once()
    for table in ("audit_log", "outbox", "risk_cases", "obligations"):
        with engine.begin() as c:
            c.execute(sa.text(f"DELETE FROM {table}"))
    assert once() == first


# --------------------------------------------- §12.5.4: simulator separation


def test_no_agent_code_path_imports_the_simulator():
    """§12.5.4 item 4, verbatim: "a test asserts no agent code path imports simulator
    internals". The moment ``flow`` or a detector can read ``sim.anchors``, the agent
    can condition on the hidden outcome table and the experiment measures itself.
    """
    offences = []
    for path in sorted((_REPO_ROOT / "reclaim").rglob("*.py")):
        if "sim" in path.relative_to(_REPO_ROOT / "reclaim").parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "reclaim.sim"
            ):
                offences.append(f"{path.name} imports {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("reclaim.sim"):
                        offences.append(f"{path.name} imports {alias.name}")
    assert offences == [], offences


def test_the_simulator_does_not_reach_back_into_the_agent():
    """The other direction, which nothing else would catch: the simulator must read
    the *spine* (a case's state, the outbox row the agent produced), never the
    router, the diagnostician, the policy engine or ``flow``. Reading those would
    let the environment resolve an outcome from the agent's reasoning instead of
    from the action it actually took."""
    forbidden = ("reclaim.flow", "reclaim.policy", "reclaim.diagnosis", "reclaim.normalize")
    for path in sorted((_REPO_ROOT / "reclaim" / "sim").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = (
                node.module
                if isinstance(node, ast.ImportFrom)
                else None
            )
            if module and module.startswith(forbidden):
                pytest.fail(f"{path.name} imports {module}")


def test_the_two_denominators_now_agree_for_every_arm_that_is_scored(conn):
    """This test used to assert the *defect*: A4 resolved a minority of its own
    cases, so its recovered rupees were divided by a denominator covering cases it
    never got a number for, and the all-cases rate read as though the agent had
    lost money it never touched. The natural-recovery floor closes that gap -- every
    in-scope case now resolves -- and this is where that is pinned. If a future
    change reintroduces an unresolved in-scope case, the equality below fails, and
    it should: the two rates would silently diverge again.

    ``simulated_at_risk`` stays on the tally rather than being deleted, because it
    is still the honest denominator for an arm that is *not* scored: A2/A3/A5 have
    cases and no outcomes at all.
    """
    cases = seed.generate(conn, n=200)
    flow.run(conn, cases)
    resolution = outcomes.resolve_batch(conn, cases)

    for arm, tally in resolution.by_arm.items():
        mine = [o for o in resolution.outcomes if o.arm is arm]
        simulated = [o for o in mine if o.was_simulated]
        assert tally.simulated_at_risk.paise == sum(
            o.amount_at_risk.paise for o in simulated
        )
        assert tally.simulated_at_risk <= tally.total_at_risk
        assert tally.gross_recovered <= tally.simulated_at_risk

    for arm in outcomes.IN_SCOPE_ARMS:
        tally = resolution.by_arm[arm]
        assert tally.simulated_case_count == tally.case_count, arm
        assert tally.simulated_at_risk == tally.total_at_risk, arm
        assert (
            tally.gross_recovered_per_simulated_rupee
            == tally.gross_recovered_per_rupee_at_risk
        ), arm

    for arm in (Arm.A2, Arm.A3, Arm.A5):
        tally = resolution.by_arm[arm]
        assert tally.simulated_at_risk < tally.total_at_risk, arm


def test_an_arm_that_resolved_nothing_has_no_simulated_rate_rather_than_zero(conn):
    """JC-33: ``None`` is the absence of a rate, ``0`` is the assertion that the arm
    recovered nothing. A2/A3/A5 were never simulated, so they have no rate at all --
    reporting 0 would put a made-up number on an arm nobody built."""
    cases = seed.generate(conn, n=200)
    flow.run(conn, cases)
    resolution = outcomes.resolve_batch(conn, cases)
    for arm in (Arm.A2, Arm.A3, Arm.A5):
        tally = resolution.by_arm[arm]
        assert tally.simulated_case_count == 0
        assert tally.simulated_at_risk == Money.zero()
        assert tally.gross_recovered_per_simulated_rupee is None


# ------------------------------------------- the natural-recovery floor (ITT)


def test_a_case_the_agent_never_acted_on_still_gets_its_natural_recovery_floor(
    conn, make_obligation, make_case
):
    """The defect this closes: an A4 case that escalated, was denied, or is parked
    for approval used to resolve to *nothing*, and the intent-to-treat estimator
    then scored it as zero recovered. Natural recovery does not switch off because
    the agent declined to act -- zero is the wrong floor, and it understates the
    treatment arm by exactly the money those cases would have collected on their
    own. The honest floor is the same natural probability arm A0 gets.
    """
    case = _open(
        conn, make_obligation, make_case, case_id="case_0501", arm=Arm.A4,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    case_machine.transition(conn, case.case_id, CaseState.DIAGNOSING)
    case_machine.transition(conn, case.case_id, CaseState.ESCALATED)

    result = outcomes.resolve_case(conn, case.case_id)
    assert result.lane is outcomes.SimLane.NATURAL_FLOOR
    assert result.was_simulated is True
    assert result.probability == anchors.natural_probability(
        DeclineClass.INSUFFICIENT_FUNDS, None
    )


def test_the_natural_floor_credits_no_action_because_none_was_taken(
    conn, make_obligation, make_case
):
    """It is a *floor*, not a consolation prize: no uplift, no contact, no verb.
    Crediting an escalated case with A4's targeted uplift would pay the agent for
    work a human has not done yet."""
    case = _open(
        conn, make_obligation, make_case, case_id="case_0502", arm=Arm.A4,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    case_machine.transition(conn, case.case_id, CaseState.DIAGNOSING)
    case_machine.transition(conn, case.case_id, CaseState.ESCALATED)

    result = outcomes.resolve_case(conn, case.case_id)
    nsf = DeclineClass.INSUFFICIENT_FUNDS
    assert result.action_type is None
    assert result.simulated_contacts == 0
    assert result.probability < anchors.generic_probability(nsf, None)
    assert result.probability < anchors.targeted_probability(
        nsf, None, ActionType.SCHEDULE_DEBIT
    )


def test_a_parked_approval_case_that_self_heals_is_stopped_as_already_paid(
    conn, make_obligation, make_case
):
    """The payer paid while the human was still deciding. The case must NOT end in
    ``SCHEDULED`` -- that would be the simulator granting the T2 approval §14.2
    reserves for a person. ``STOPPED(already_paid)`` is what actually happened."""
    case_id = _case_id_with_draw(lambda d: d < Decimal("0.02"), Arm.A4)
    case = _open(
        conn, make_obligation, make_case, case_id=case_id, arm=Arm.A4,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    case_machine.transition(conn, case.case_id, CaseState.DIAGNOSING)
    case_machine.transition(conn, case.case_id, CaseState.PLANNED)
    case_machine.transition(conn, case.case_id, CaseState.AWAITING_APPROVAL)

    result = outcomes.resolve_case(conn, case.case_id)
    assert result.lane is outcomes.SimLane.NATURAL_FLOOR
    assert result.recovered is True
    stored = ledger.get_case(conn, case.case_id)
    assert stored.state is CaseState.STOPPED
    assert stored.stop_reason is StopReason.ALREADY_PAID


def test_an_escalated_case_that_self_heals_reaches_recovered(
    conn, make_obligation, make_case
):
    """§9.1 does have ``ESCALATED -> RECOVERED``, so unlike the A0 compromise this
    one needs no workaround: the state and the money agree."""
    case_id = _case_id_with_draw(lambda d: d < Decimal("0.02"), Arm.A4)
    case = _open(
        conn, make_obligation, make_case, case_id=case_id, arm=Arm.A4,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    case_machine.transition(conn, case.case_id, CaseState.DIAGNOSING)
    case_machine.transition(conn, case.case_id, CaseState.ESCALATED)

    result = outcomes.resolve_case(conn, case.case_id)
    assert result.recovered is True
    assert result.final_state is CaseState.RECOVERED
    assert ledger.get_case(conn, case.case_id).state is CaseState.RECOVERED


def test_a_policy_denied_case_is_terminal_so_the_amount_rides_on_the_outcome(
    conn, make_obligation, make_case
):
    """``STOPPED`` has no outgoing edges by construction. A denied case that later
    self-heals therefore cannot be moved, and the recovered amount lives on the
    ``SimulatedOutcome`` -- the same compromise A0 carries, for the same reason."""
    case_id = _case_id_with_draw(lambda d: d < Decimal("0.02"), Arm.A4)
    case = _open(
        conn, make_obligation, make_case, case_id=case_id, arm=Arm.A4,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    case_machine.transition(
        conn, case.case_id, CaseState.STOPPED,
        stop_reason=StopReason.POLICY_BLOCKED, stopped_at=case.detected_at,
    )
    result = outcomes.resolve_case(conn, case.case_id)
    assert result.lane is outcomes.SimLane.NATURAL_FLOOR
    assert result.recovered is True
    assert result.recovered_amount == case.amount_at_risk
    assert result.final_state is CaseState.STOPPED
    assert ledger.get_case(conn, case.case_id).stop_reason is StopReason.POLICY_BLOCKED


def test_every_in_scope_case_is_resolved_exactly_once_on_a_seeded_batch(conn):
    """The property the whole ITT estimator rests on: no case in A0/A1/A4 comes back
    unresolved, and none is resolved twice."""
    cases = seed.generate(conn, n=200)
    flow.run(conn, cases)
    resolution = outcomes.resolve_batch(conn, cases)

    in_scope = [o for o in resolution.outcomes if o.arm in outcomes.IN_SCOPE_ARMS]
    assert in_scope, "the seeded batch put no case in A0/A1/A4"
    assert all(o.was_simulated for o in in_scope)
    assert all(
        o.lane is outcomes.SimLane.NOT_SIMULATED
        for o in resolution.outcomes
        if o.arm not in outcomes.IN_SCOPE_ARMS
    )
    assert len(_sim_rows(conn)) == len(in_scope)


def test_an_out_of_scope_arm_gets_no_floor_either(conn, make_obligation, make_case):
    """A2/A3/A5 are not scored, so giving them a natural floor would manufacture a
    number for an arm nobody built (§18.4)."""
    case = _open(
        conn, make_obligation, make_case, case_id="case_0503", arm=Arm.A3,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    result = outcomes.resolve_case(conn, case.case_id)
    assert result.lane is outcomes.SimLane.NOT_SIMULATED
    assert result.probability is None


def test_a_second_pass_is_recognised_from_the_audit_log_not_from_the_state(
    conn, make_obligation, make_case
):
    """Idempotency used to be a side effect of the entry-state check, which the
    natural floor breaks: a resolved-and-stopped case is no longer in any entry
    state, but it *is* still in an arm, so a state-only check would hand it a second
    draw. The ``simulated_psp_response`` row is the authoritative record that a case
    has been resolved, so that is what gets checked."""
    case = _open(
        conn, make_obligation, make_case, case_id="case_0504", arm=Arm.A0,
        canonical_decline_class=DeclineClass.INSUFFICIENT_FUNDS,
    )
    first = outcomes.resolve_case(conn, case.case_id)
    assert first.was_simulated is True

    second = outcomes.resolve_case(conn, case.case_id)
    assert second.lane is outcomes.SimLane.NOT_SIMULATED
    assert "already" in second.reason
    assert len(_sim_rows(conn, case.case_id)) == 1
