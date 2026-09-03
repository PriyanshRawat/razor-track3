"""§12.1's headline, computed: net incremental recovery with a bootstrap CI.

The arithmetic here is short. What these tests guard is the handful of places where a
scoreboard can be *arithmetically correct and still a lie*:

* **A point estimate never appears without its interval** (JC-36). The frozen
  ``IncrementalRecoveryEstimate`` already refuses half an interval; the renderer must
  refuse to print one too.
* **A reduced resample count is not silently publishable.** §12.1 specifies 10,000
  resamples. ``is_publishable_as_headline`` is ``True`` only at that count, so a
  1,000-resample demo number is *constructible* but marked -- and the render says so.
* **The unit is the case as randomised** (§12.1). Cases the agent never acted on stay
  in the denominator; dropping them is a per-protocol analysis of a randomised
  experiment, which is the exact bias randomisation exists to remove. Both bases are
  computable, they disagree on the seeded batch, and the scoreboard says which one it
  used.
* **A rate with no denominator is ``None``, not zero** (JC-33), and a sample too small
  to support inference is flagged rather than quietly reported.
* **The interval behaves like a bootstrap interval**: it brackets the point, it
  narrows as n grows, it spans zero on a null effect and excludes zero on a strong
  one, and it is reproducible across processes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from reclaim.contracts.decline_taxonomy import DeclineClass
from reclaim.contracts.enums import (
    HEADLINE_CONTROL_ARM,
    HEADLINE_TREATMENT_ARM,
    Arm,
    CaseState,
    RiskClass,
)
from reclaim.contracts.metrics import BOOTSTRAP_RESAMPLES, recovery_rate
from reclaim.contracts.money import Money
from reclaim.sim import outcomes as sim_outcomes
from reclaim.sim import scoreboard as sb
from reclaim.spine import seed
from reclaim import flow

_REPO_ROOT = Path(__file__).resolve().parents[1]

DEMO_RESAMPLES = 1_000


# ----------------------------------------------------------------- helpers


def _out(
    case_id: str,
    arm: Arm,
    *,
    rupees: int = 1000,
    recovered: bool = False,
    simulated: bool = True,
) -> sim_outcomes.SimulatedOutcome:
    """A ``SimulatedOutcome`` built by hand, so a test can state the rates it wants.

    ``simulated=False`` is the shape the scoreboard has to decide about: a case the
    experiment randomised into an arm but the agent never acted on.
    """
    amount = Money.from_rupees(rupees)
    lane = (
        sim_outcomes.SimLane.NOT_SIMULATED
        if not simulated
        else {
            Arm.A0: sim_outcomes.SimLane.NATURAL_ONLY,
            Arm.A1: sim_outcomes.SimLane.GENERIC_BASELINE,
        }.get(arm, sim_outcomes.SimLane.TARGETED_AGENT)
    )
    return sim_outcomes.SimulatedOutcome(
        case_id=case_id,
        arm=arm,
        lane=lane,
        decline_class=DeclineClass.INSUFFICIENT_FUNDS,
        risk_class=RiskClass.FAILED_RECURRING_DEBIT,
        amount_at_risk=amount,
        entry_state=CaseState.DETECTED,
        final_state=CaseState.RECOVERED if recovered else CaseState.DETECTED,
        action_type=None,
        recovered=recovered,
        recovered_amount=amount if recovered else Money.zero(),
        probability=Decimal("0.500000") if simulated else None,
        draw=Decimal("0.100000") if simulated else None,
        simulated_contacts=0,
        reason="hand-built",
    )


def _batch(spec: dict[Arm, tuple[int, int]], *, rupees: int = 1000):
    """``{arm: (case_count, recovered_count)}`` -> a flat list of outcomes.

    Amounts are deliberately varied. A batch where every case is worth the same
    puts the resampled recovery rate on a lattice of k/n, which is coarse enough to
    hide the bootstrap's seed dependence and to make an interval look exact.
    """
    built = []
    for arm, (total, recovered) in spec.items():
        for i in range(total):
            built.append(
                _out(
                    f"case_{arm.value.lower()}{i:04d}",
                    arm,
                    rupees=rupees * (1 + i % 9),
                    recovered=i < recovered,
                )
            )
    return built


def _seeded(conn, n=200):
    cases = seed.generate(conn, n=n)
    flow.run(conn, cases)
    return sim_outcomes.resolve_batch(conn, cases).outcomes


# ------------------------------------------------------------- arm scores


def test_the_scoreboard_covers_only_the_three_arms_in_scope():
    board = sb.build_scoreboard(
        _batch({Arm.A0: (40, 10), Arm.A1: (40, 14), Arm.A4: (40, 24)})
        + [_out("case_a2", Arm.A2), _out("case_a5", Arm.A5)],
        resamples=DEMO_RESAMPLES,
    )
    assert set(board.arms) == {Arm.A0, Arm.A1, Arm.A4}
    assert sb.SCOREBOARD_ARMS == (Arm.A0, Arm.A1, Arm.A4)
    assert any("A2" in w and "A5" in w for w in board.warnings)


def test_the_recovery_rate_per_arm_is_the_frozen_metric_not_a_local_division():
    """Re-derived from §13's definition (recovered obligations / at-risk
    obligations) through the frozen helper, so a local shortcut in the scoreboard
    would disagree here."""
    board = sb.build_scoreboard(
        _batch({Arm.A0: (40, 10), Arm.A1: (40, 14), Arm.A4: (40, 24)}),
        resamples=DEMO_RESAMPLES,
    )
    for arm, expected in ((Arm.A0, 10), (Arm.A1, 14), (Arm.A4, 24)):
        assert board.arms[arm].recovery_rate == recovery_rate(
            recovered_obligations=expected, at_risk_obligations=40
        )


def test_the_per_rupee_rate_is_gross_because_no_cost_is_modelled():
    """§13's headline differences the *net* rate. With no cost inputs, net == gross
    and the scoreboard has to say so rather than let "net" ride on a zero."""
    board = sb.build_scoreboard(
        _batch({Arm.A0: (40, 10), Arm.A1: (40, 14), Arm.A4: (40, 24)}),
        resamples=DEMO_RESAMPLES,
    )
    for score in board.arms.values():
        assert score.outcome.cost.total.is_zero
        assert score.outcome.net_recovered == score.outcome.gross_recovered
    assert any("cost" in w.lower() for w in board.warnings)


def test_an_arm_with_no_cases_is_refused_rather_than_scored_zero():
    """JC-33: an arm with nothing in it has no rate. Scoring it zero would put a
    number on an empty cell and let it into a difference."""
    with pytest.raises(ValueError):
        sb.build_scoreboard(
            _batch({Arm.A0: (0, 0), Arm.A4: (10, 5)}), resamples=DEMO_RESAMPLES
        )


# --------------------------------------------------------------- the headline


def test_the_headline_is_the_documented_difference_times_total_at_risk():
    """§13, verbatim: ``(net/at risk)_t - (net/at risk)_c, x total at risk``.
    Computed here from the arm totals rather than read back."""
    board = sb.build_scoreboard(
        _batch({Arm.A0: (40, 10), Arm.A1: (40, 14), Arm.A4: (40, 24)}),
        resamples=DEMO_RESAMPLES,
    )
    a0, a4 = board.arms[Arm.A0], board.arms[Arm.A4]
    expected_delta = (
        a4.gross_recovered.ratio_to(a4.total_at_risk)
        - a0.gross_recovered.ratio_to(a0.total_at_risk)
    )
    estimate = board.headline.estimate
    assert estimate.per_rupee_delta == pytest.approx(expected_delta, abs=Decimal("0.000004"))
    assert estimate.total_at_risk == board.total_at_risk
    assert estimate.point == board.total_at_risk * estimate.per_rupee_delta


def test_the_headline_control_is_a0_and_the_plan_headline_control_is_a1():
    """Two different comparisons, both real. §17's script leads with "money that
    comes back with no help", which is A0; §12.1 and ``enums.HEADLINE_CONTROL_ARM``
    name A1 as the control for the *reported* estimate. Conflating them would let
    "vs doing nothing" be presented as "vs the industry baseline"."""
    board = sb.build_scoreboard(
        _batch({Arm.A0: (40, 10), Arm.A1: (40, 14), Arm.A4: (40, 24)}),
        resamples=DEMO_RESAMPLES,
    )
    assert HEADLINE_CONTROL_ARM is Arm.A1
    assert HEADLINE_TREATMENT_ARM is Arm.A4
    assert board.headline.estimate.control_arm is Arm.A0
    assert board.headline.estimate.treatment_arm is Arm.A4
    assert board.vs_baseline.estimate.control_arm is HEADLINE_CONTROL_ARM
    labels = {c.label for c in board.comparisons}
    assert len(labels) == 2


def test_a_headline_carries_a_complete_interval_and_its_resample_count():
    board = sb.build_scoreboard(
        _batch({Arm.A0: (40, 10), Arm.A1: (40, 14), Arm.A4: (40, 24)}),
        resamples=DEMO_RESAMPLES,
    )
    for comparison in board.comparisons:
        estimate = comparison.estimate
        assert estimate.ci_low is not None and estimate.ci_high is not None
        assert estimate.ci_low <= estimate.ci_high
        assert estimate.resamples == DEMO_RESAMPLES
        assert estimate.p_value is not None


def test_a_reduced_resample_count_is_computable_but_not_publishable():
    """The collision worth knowing about: §12.1 says 10,000 and the frozen
    ``is_publishable_as_headline`` checks for exactly that. A 1,000-resample number
    is fine to compute and to demo -- it is just not the headline, and the contract
    is what says so, not a comment."""
    batch = _batch({Arm.A0: (40, 10), Arm.A1: (40, 14), Arm.A4: (40, 24)})
    demo = sb.build_scoreboard(batch, resamples=DEMO_RESAMPLES)
    assert demo.headline.estimate.is_publishable_as_headline is False
    assert demo.headline.meets_plan_resample_count is False
    assert any(str(DEMO_RESAMPLES) in w for w in demo.warnings)

    full = sb.build_scoreboard(batch, resamples=BOOTSTRAP_RESAMPLES)
    assert full.headline.estimate.is_publishable_as_headline is True
    assert full.headline.meets_plan_resample_count is True


def test_the_default_resample_count_is_the_plan_s_number():
    """The default must be the publishable one. A default of 1,000 would make every
    caller who did not think about it produce an unpublishable estimate."""
    assert sb.DEFAULT_RESAMPLES == BOOTSTRAP_RESAMPLES == 10_000


def test_the_stratum_weighted_estimator_is_not_used_and_the_board_says_so():
    """§12.1 specifies a stratum-weighted estimate. This is pooled -- one cell --
    and the frozen field that records that is ``strata_count``."""
    board = sb.build_scoreboard(
        _batch({Arm.A0: (40, 10), Arm.A1: (40, 14), Arm.A4: (40, 24)}),
        resamples=DEMO_RESAMPLES,
    )
    assert board.headline.estimate.strata_count == 1
    assert any("stratum" in w.lower() or "strat" in w.lower() for w in board.warnings)


# ------------------------------------------------------------- the bootstrap


def test_the_interval_brackets_the_point_estimate():
    board = sb.build_scoreboard(
        _batch({Arm.A0: (200, 40), Arm.A1: (200, 60), Arm.A4: (200, 120)}),
        resamples=DEMO_RESAMPLES,
    )
    estimate = board.headline.estimate
    assert estimate.ci_low <= estimate.point <= estimate.ci_high


def test_the_bootstrap_is_deterministic_for_a_given_seed():
    batch = _batch({Arm.A0: (60, 15), Arm.A1: (60, 22), Arm.A4: (60, 35)})
    first = sb.build_scoreboard(batch, resamples=DEMO_RESAMPLES)
    second = sb.build_scoreboard(batch, resamples=DEMO_RESAMPLES)
    assert first.headline.estimate.ci_low == second.headline.estimate.ci_low
    assert first.headline.estimate.ci_high == second.headline.estimate.ci_high
    assert first.headline.estimate.p_value == second.headline.estimate.p_value


def test_a_different_bootstrap_seed_moves_the_interval_but_not_the_point():
    batch = _batch({Arm.A0: (60, 15), Arm.A1: (60, 22), Arm.A4: (60, 35)})
    a = sb.build_scoreboard(batch, resamples=DEMO_RESAMPLES, bootstrap_seed=1)
    b = sb.build_scoreboard(batch, resamples=DEMO_RESAMPLES, bootstrap_seed=2)
    assert a.headline.estimate.point == b.headline.estimate.point
    assert a.headline.estimate.ci_low != b.headline.estimate.ci_low


def test_more_resamples_do_not_move_the_point_estimate():
    """The point is a property of the data. If it moved with the resample count the
    bootstrap would be feeding back into the estimate it is supposed to bound."""
    batch = _batch({Arm.A0: (60, 15), Arm.A1: (60, 22), Arm.A4: (60, 35)})
    small = sb.build_scoreboard(batch, resamples=200)
    large = sb.build_scoreboard(batch, resamples=2_000)
    assert small.headline.estimate.point == large.headline.estimate.point
    assert small.headline.estimate.per_rupee_delta == large.headline.estimate.per_rupee_delta


def test_the_interval_narrows_as_the_sample_grows():
    def width(n):
        board = sb.build_scoreboard(
            _batch({Arm.A0: (n, n // 4), Arm.A1: (n, n // 3), Arm.A4: (n, n // 2)}),
            resamples=DEMO_RESAMPLES,
        )
        estimate = board.headline.estimate
        # Per-rupee, so the two runs are comparable despite different totals.
        return (estimate.ci_high - estimate.ci_low).ratio_to(estimate.total_at_risk)

    assert width(400) < width(40)


def test_a_null_effect_produces_an_interval_that_spans_zero():
    board = sb.build_scoreboard(
        _batch({Arm.A0: (300, 90), Arm.A1: (300, 90), Arm.A4: (300, 90)}),
        resamples=DEMO_RESAMPLES,
    )
    estimate = board.headline.estimate
    assert estimate.ci_low <= Money.zero() <= estimate.ci_high
    assert estimate.p_value > Decimal("0.05")


def test_a_strong_effect_produces_an_interval_that_excludes_zero():
    board = sb.build_scoreboard(
        _batch({Arm.A0: (300, 30), Arm.A1: (300, 45), Arm.A4: (300, 240)}),
        resamples=DEMO_RESAMPLES,
    )
    estimate = board.headline.estimate
    assert estimate.ci_low > Money.zero()
    assert estimate.p_value < Decimal("0.05")


def test_a_negative_effect_is_reported_as_negative_not_clamped():
    """An arm that recovers less than its control must produce a negative headline.
    Clamping at zero would hide exactly the result that matters most."""
    board = sb.build_scoreboard(
        _batch({Arm.A0: (300, 200), Arm.A1: (300, 100), Arm.A4: (300, 40)}),
        resamples=DEMO_RESAMPLES,
    )
    estimate = board.headline.estimate
    assert estimate.per_rupee_delta < Decimal(0)
    assert estimate.point < Money.zero()
    assert estimate.ci_high < Money.zero()


def test_the_bootstrap_is_stable_across_processes():
    """Same discipline as the arm assigner (invariant #5). The bootstrap uses an
    explicitly seeded ``random.Random``, so this holds; a set-iteration order or a
    builtin ``hash()`` anywhere in the path would break it."""
    body = (
        "b = build_scoreboard(_batch({Arm.A0:(60,15),Arm.A1:(60,22),Arm.A4:(60,35)}), "
        "resamples=500)\n"
        "e = b.headline.estimate\n"
        "print(e.ci_low.paise, e.ci_high.paise, e.p_value)"
    )
    preamble = (
        "import sys; sys.path.insert(0, %r)\n"
        "from tests.test_sim_scoreboard import _batch\n"
        "from reclaim.contracts.enums import Arm\n"
        "from reclaim.sim.scoreboard import build_scoreboard\n"
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


# ------------------------------------------------------------------ the basis


def test_the_default_basis_keeps_every_randomised_case_in_the_denominator():
    """§12.1's unit is the obligation-case as assigned. A case the agent never acted
    on recovered nothing, and it stays in the arm it was randomised into."""
    batch = [
        _out("case_a", Arm.A0, recovered=True),
        _out("case_b", Arm.A0),
        _out("case_c", Arm.A4, recovered=True),
        _out("case_d", Arm.A4, simulated=False),
        _out("case_e", Arm.A1, recovered=True),
        _out("case_f", Arm.A1),
    ]
    board = sb.build_scoreboard(batch, resamples=200)
    assert board.basis is sb.Basis.ALL_RANDOMISED
    assert board.arms[Arm.A4].case_count == 2
    assert board.arms[Arm.A4].resolved_case_count == 1
    assert board.arms[Arm.A4].total_at_risk == Money.from_rupees(2000)


def test_the_resolved_only_basis_drops_the_unacted_cases_and_labels_itself():
    batch = [
        _out("case_a", Arm.A0, recovered=True),
        _out("case_b", Arm.A0),
        _out("case_c", Arm.A4, recovered=True),
        _out("case_d", Arm.A4, simulated=False),
        _out("case_e", Arm.A1, recovered=True),
        _out("case_f", Arm.A1),
    ]
    board = sb.build_scoreboard(batch, basis=sb.Basis.RESOLVED_ONLY, resamples=200)
    assert board.basis is sb.Basis.RESOLVED_ONLY
    assert board.arms[Arm.A4].case_count == 1
    assert board.arms[Arm.A4].total_at_risk == Money.from_rupees(1000)
    assert any("per-protocol" in w.lower() for w in board.warnings)


def test_the_two_bases_disagree_on_the_seeded_batch(conn):
    """Not a hypothetical: the flow resolves a minority of A4's cases, so the choice
    of basis changes the sign of the headline. A scoreboard that offered only one of
    them would be making that choice invisibly."""
    resolved = _seeded(conn, n=200)
    itt = sb.build_scoreboard(resolved, resamples=DEMO_RESAMPLES)
    per_protocol = sb.build_scoreboard(
        resolved, basis=sb.Basis.RESOLVED_ONLY, resamples=DEMO_RESAMPLES
    )
    assert itt.headline.estimate.point != per_protocol.headline.estimate.point
    assert itt.arms[Arm.A4].case_count > per_protocol.arms[Arm.A4].case_count


def test_an_arm_below_the_inference_floor_is_flagged_by_name(conn):
    resolved = _seeded(conn, n=200)
    board = sb.build_scoreboard(
        resolved, basis=sb.Basis.RESOLVED_ONLY, resamples=DEMO_RESAMPLES
    )
    flagged = " ".join(board.warnings)
    assert str(sb.MIN_CASES_FOR_INFERENCE) in flagged
    assert "A4" in flagged


# ------------------------------------------------------------------ the render


def test_the_render_shows_each_arm_with_its_recovered_total(conn):
    board = sb.build_scoreboard(_seeded(conn, n=200), resamples=DEMO_RESAMPLES)
    text = board.render()
    for arm in sb.SCOREBOARD_ARMS:
        assert arm.value in text
    assert "at risk" in text.lower()
    assert "recovered" in text.lower()


def test_the_render_never_shows_a_point_without_its_interval(conn):
    """JC-36 as a display rule. Every line carrying the point estimate must carry
    the interval on the same line."""
    board = sb.build_scoreboard(_seeded(conn, n=200), resamples=DEMO_RESAMPLES)
    for line in board.render().splitlines():
        if "incremental" in line.lower() and "=" in line:
            assert "95% CI" in line, line


def test_the_render_states_what_is_not_being_claimed(conn):
    """§12.4 requires an explicit "what we are NOT claiming" slide. The scoreboard
    prints its own, because the caveats travel with the number or they get lost."""
    board = sb.build_scoreboard(_seeded(conn, n=200), resamples=DEMO_RESAMPLES)
    text = board.render().lower()
    assert "not claiming" in text
    assert "no real customer money" in text or "not real" in text
    assert "simulated" in text
    assert "gross" in text  # no cost-to-collect, so this is not §13's net figure
    assert str(DEMO_RESAMPLES) in board.render()


def test_the_render_marks_a_demo_interval_as_not_the_publishable_headline(conn):
    resolved = _seeded(conn, n=200)
    demo = sb.build_scoreboard(resolved, resamples=DEMO_RESAMPLES)
    assert "not publishable" in demo.render().lower()
    full = sb.build_scoreboard(resolved, resamples=BOOTSTRAP_RESAMPLES)
    assert "not publishable" not in full.render().lower()


def test_the_scoreboard_runs_end_to_end_on_the_seeded_batch(conn):
    board = sb.build_scoreboard(_seeded(conn, n=200), resamples=DEMO_RESAMPLES)
    assert board.arms[Arm.A0].case_count > 0
    assert board.arms[Arm.A1].case_count > 0
    assert board.arms[Arm.A4].case_count > 0
    assert board.total_at_risk.is_positive
    assert board.render().strip()
