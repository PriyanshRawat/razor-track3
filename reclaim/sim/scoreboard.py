"""§12.1's headline, computed from resolved outcomes: net incremental recovery + CI.

``(recovered / at risk)_treatment - (recovered / at risk)_control, x total at risk``,
with a percentile bootstrap 95% interval, plus §13's recovery rate per arm. Arms
A0/A1/A4 only -- the T-12h scope of ``sim.outcomes``.

This module lives inside ``reclaim/sim/`` deliberately. It is the evaluator, not the
environment, but it consumes simulated outcomes and nothing else, and keeping it here
means the existing separation test (§12.5.4 item 4) already forbids any agent code
path from importing it. A scoreboard a detector can read is a scoreboard a detector
can optimise against.

Five things a reader of these numbers has to know
------------------------------------------------
1. **Every figure is GROSS, not net.** §13 defines the headline on *net* recovered
   (gross minus cost to collect), and no cost inputs exist: there is no PSP fee
   model, no channel cost, no LLM cost meter and no human-minutes clock. The
   ``CostBreakdown`` attached to each arm is all zeros, so ``net_recovered`` and
   ``gross_recovered`` are the same number here. That is a *missing* term, not a
   zero one -- adding real costs can only move the headline down, and it moves the
   arms unequally, because A1 sends four times the contacts A4 does and A4 is the
   only arm that spends human approval minutes.
2. **It is pooled, not stratum-weighted.** §12.1 specifies a stratum-weighted
   estimator, and ``metrics.stratum_weighted_incremental_recovery`` exists to
   compute it -- but it refuses a stratum present in only one arm, and at this batch
   size almost no ``amount band x failure class x segment`` cell is populated in both
   A4 and A0. Pooling is the honest fallback; ``strata_count=1`` on the frozen
   estimate is what records that it happened.
3. **The default basis is intent-to-treat.** §12.1's unit is "the obligation-case",
   assigned at creation and immutable. A case the agent escalated, was denied, or is
   holding for approval recovered nothing, and it stays in the denominator of the arm
   it was randomised into. ``Basis.RESOLVED_ONLY`` computes the per-protocol figure
   instead, and it is available precisely so the gap between the two is visible: on
   the seeded batch they disagree about the *sign* of the effect, because the flow
   resolves a minority of A4's cases and the rest score zero.
4. **The resample count is a knob with a contract attached.** ``DEFAULT_RESAMPLES``
   is §12.1's 10,000. A smaller count is computable and fine for a demo -- but
   ``IncrementalRecoveryEstimate.is_publishable_as_headline`` is ``True`` only at
   10,000, so a reduced-resample interval is marked as not publishable by the frozen
   contract rather than by a comment here. Measured cost of the full count on a
   200-case batch is a couple of seconds, so the reduction buys very little.
5. **Two controls, both reported.** §17's opening beat contrasts the agent with
   "money that comes back with no help" -- that is A0. §12.1 and
   ``enums.HEADLINE_CONTROL_ARM`` name **A1** as the control for the reported
   estimate, because a fixed drip is what a real merchant already runs. A4-A0 is the
   bigger number and the weaker claim; both are computed, and each is labelled with
   which it is.

The bootstrap
-------------
Case-level, resampled with replacement within each arm independently (the arms are
independent samples), 2.5th/97.5th percentiles of the resampled per-rupee delta,
scaled by the **observed** total at risk. The total is held fixed rather than
resampled: it is the population quantity the rate is projected onto, and resampling
it would fold the denominator's sampling variability into the interval twice.

``p_value`` is a two-sided bootstrap percentile p -- ``2 x min(P(delta*<=0),
P(delta*>=0))`` -- not a t-test and not an exact test. With an arm under
``MIN_CASES_FOR_INFERENCE`` cases it should be read as decoration; the scoreboard
emits a warning naming the arm when that happens.

Float discipline: the resampling loop runs in floats, which is what invariant #2
permits for internal computation ("bootstrap, model inference, EWMA"), and converts
once at the boundary -- the percentiles become ``Ratio`` and ``Money`` before they
touch the frozen estimate or an audit row. Determinism comes from an explicitly
seeded ``random.Random``, the same choice ``spine.seed`` makes; nothing here calls
the builtin ``hash()`` or iterates a set.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, Mapping, Sequence

from reclaim.contracts.enums import (
    HEADLINE_CONTROL_ARM,
    AmountBand,
    Arm,
    RiskClass,
    Segment,
)
from reclaim.contracts.metrics import (
    BOOTSTRAP_RESAMPLES,
    ArmOutcome,
    CostBreakdown,
    IncrementalRecoveryEstimate,
    net_incremental_recovery,
    net_recovered_per_rupee_at_risk,
    recovery_rate,
)
from reclaim.contracts.money import Money, money_sum
from reclaim.contracts.strata import StratumKey
from reclaim.contracts.units import pvalue, ratio
from reclaim.sim.anchors import ANCHOR_HONESTY_NOTE, ANCHORS_VERSION
from reclaim.sim.outcomes import SIM_SALT, SimulatedOutcome

__all__ = [
    "ArmScore",
    "Basis",
    "Comparison",
    "DEFAULT_RESAMPLES",
    "MIN_CASES_FOR_INFERENCE",
    "POOLED_STRATUM",
    "SCOREBOARD_ARMS",
    "Scoreboard",
    "build_scoreboard",
]

#: The arms this scoreboard reports, in display order. Same scope as
#: ``sim.outcomes.IN_SCOPE_ARMS`` (§18.4's T-12h cut), ordered as the ablation
#: ladder reads rather than as a set.
SCOREBOARD_ARMS: Final[tuple[Arm, ...]] = (Arm.A0, Arm.A1, Arm.A4)

#: §12.1's number. The default on purpose: a default of 1,000 would hand every
#: caller who did not think about it an estimate the frozen contract calls
#: unpublishable.
DEFAULT_RESAMPLES: Final[int] = BOOTSTRAP_RESAMPLES

#: Seed for the resampling RNG. Fixed so a scoreboard is reproducible from the
#: batch alone; it is *not* the experiment salt or the simulator salt, and changing
#: it moves the interval without moving the point.
BOOTSTRAP_SEED: Final[int] = 20260903

#: Below this many cases in an arm, the interval is arithmetic rather than
#: inference. Not a significance threshold -- a flag, so a 9-case arm cannot be
#: quoted as though it were 900.
MIN_CASES_FOR_INFERENCE: Final[int] = 30

#: A pooled ``ArmOutcome`` still needs a ``StratumKey``, because Phase 0 froze
#: ``ArmOutcome`` as a *per-stratum* record (§12.1) and there is no pooled variant.
#: This triple is a sentinel: ``spine.seed`` cannot produce a case in it (it draws
#: no amount above Rs 10 L, never uses ``B2B_STRATEGIC``, and has no D6 detector),
#: so a pooled row can never be mistaken for a real stratum. Nothing on the pooled
#: path reads it -- ``net_incremental_recovery`` differences rates, never strata.
#: When the batch is big enough for ``stratum_weighted_incremental_recovery``, this
#: constant and the pooling around it are deleted, not extended.
POOLED_STRATUM: Final[StratumKey] = StratumKey(
    amount_band=AmountBand.GT_10L,
    failure_class=RiskClass.SILENT_LEAKAGE.value,
    segment=Segment.B2B_STRATEGIC,
)

_ARM_LABELS: Final[Mapping[Arm, str]] = {
    Arm.A0: "no action (natural recovery)",
    Arm.A1: "fixed schedule + static 4-touch drip",
    Arm.A4: "full agent: diagnose -> policy -> act",
}


class Basis(StrEnum):
    """Which cases enter the denominators. See the docstring, point 3."""

    ALL_RANDOMISED = "all_randomised"
    RESOLVED_ONLY = "resolved_only"


@dataclass(frozen=True)
class ArmScore:
    """One arm's row on the scoreboard, plus the frozen record behind it."""

    arm: Arm
    case_count: int
    resolved_case_count: int
    recovered_case_count: int
    total_at_risk: Money
    gross_recovered: Money
    outbound_contacts: int
    outcome: ArmOutcome

    @property
    def recovery_rate(self) -> Decimal | None:
        """§13: recovered obligations / at-risk obligations, via the frozen helper."""
        return recovery_rate(
            recovered_obligations=self.recovered_case_count,
            at_risk_obligations=self.case_count,
        )

    @property
    def recovered_per_rupee_at_risk(self) -> Decimal | None:
        """The quantity the headline differences. Named ``net`` by the frozen helper
        it calls; equal to gross here because ``cost`` is all zeros."""
        return net_recovered_per_rupee_at_risk(self.outcome)


@dataclass(frozen=True)
class Comparison:
    """One treatment-minus-control estimate, and what it is a comparison *of*."""

    label: str
    estimate: IncrementalRecoveryEstimate
    treatment_case_count: int
    control_case_count: int

    @property
    def meets_plan_resample_count(self) -> bool:
        """Whether the interval was produced at §12.1's 10,000 resamples. Mirrors
        the frozen ``is_publishable_as_headline`` for the resample half of it."""
        return self.estimate.resamples == BOOTSTRAP_RESAMPLES


@dataclass(frozen=True)
class Scoreboard:
    """Everything §17's opening beat reads off, and the caveats that travel with it."""

    basis: Basis
    arms: Mapping[Arm, ArmScore]
    comparisons: tuple[Comparison, ...]
    total_at_risk: Money
    case_count: int
    resamples: int
    bootstrap_seed: int
    warnings: tuple[str, ...]

    @property
    def headline(self) -> Comparison:
        """A4 vs A0: the agent against doing nothing. §17's opening framing."""
        return self.comparisons[0]

    @property
    def vs_baseline(self) -> Comparison:
        """A4 vs A1: §12.1's reported control, and the defensible claim."""
        return self.comparisons[1]

    def render(self) -> str:
        return _render(self)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def _pooled_outcome(arm: Arm, cases: Sequence[SimulatedOutcome]) -> ArmOutcome:
    return ArmOutcome(
        arm=arm,
        stratum=POOLED_STRATUM,
        at_risk_case_count=len(cases),
        recovered_case_count=sum(1 for o in cases if o.recovered),
        total_at_risk=money_sum([o.amount_at_risk for o in cases]),
        gross_recovered=money_sum([o.recovered_amount for o in cases]),
        # All zeros, and that is the point: see the docstring, point 1.
        cost=CostBreakdown(),
        outbound_contacts=sum(o.simulated_contacts for o in cases),
    )


def _score(arm: Arm, cases: Sequence[SimulatedOutcome]) -> ArmScore:
    outcome = _pooled_outcome(arm, cases)
    return ArmScore(
        arm=arm,
        case_count=len(cases),
        resolved_case_count=sum(1 for o in cases if o.was_simulated),
        recovered_case_count=outcome.recovered_case_count,
        total_at_risk=outcome.total_at_risk,
        gross_recovered=outcome.gross_recovered,
        outbound_contacts=outcome.outbound_contacts,
        outcome=outcome,
    )


def _resampled_rate(rng: random.Random, at_risk: list[int], recovered: list[int]) -> float:
    """One bootstrap replicate of an arm's recovered-per-rupee rate.

    Floats on purpose (invariant #2 permits it for internal computation); the
    conversion to a fixed-scale ``Decimal`` happens once, on the percentiles.
    """
    n = len(at_risk)
    picks = rng.choices(range(n), k=n)
    denominator = sum(at_risk[i] for i in picks)
    numerator = sum(recovered[i] for i in picks)
    return numerator / denominator


def _bootstrap(
    treatment: Sequence[SimulatedOutcome],
    control: Sequence[SimulatedOutcome],
    *,
    resamples: int,
    seed: int,
) -> tuple[Decimal, Decimal, Decimal]:
    """``(delta_low, delta_high, p_value)`` for the per-rupee delta.

    Percentile bootstrap: the 2.5th and 97.5th order statistics of the resampled
    delta. Deliberately not BCa or studentised -- those need a variance estimate or
    a jackknife this batch size cannot support, and dressing a 9-case arm in a
    better interval estimator would misrepresent how much is known, not less.
    """
    t_at_risk = [o.amount_at_risk.paise for o in treatment]
    t_recovered = [o.recovered_amount.paise for o in treatment]
    c_at_risk = [o.amount_at_risk.paise for o in control]
    c_recovered = [o.recovered_amount.paise for o in control]

    rng = random.Random(seed)
    deltas = [
        _resampled_rate(rng, t_at_risk, t_recovered)
        - _resampled_rate(rng, c_at_risk, c_recovered)
        for _ in range(resamples)
    ]
    deltas.sort()

    low_index = int(0.025 * resamples)
    high_index = min(resamples - 1, int(0.975 * resamples))

    at_or_below_zero = sum(1 for d in deltas if d <= 0.0)
    at_or_above_zero = sum(1 for d in deltas if d >= 0.0)
    two_sided = 2.0 * min(at_or_below_zero, at_or_above_zero) / resamples

    return (
        ratio(Decimal(str(deltas[low_index]))),
        ratio(Decimal(str(deltas[high_index]))),
        pvalue(Decimal(str(min(1.0, two_sided)))),
    )


def _compare(
    label: str,
    treatment: Sequence[SimulatedOutcome],
    control: Sequence[SimulatedOutcome],
    treatment_score: ArmScore,
    control_score: ArmScore,
    *,
    total_at_risk: Money,
    resamples: int,
    seed: int,
) -> Comparison:
    """The frozen point estimate, re-validated with its interval attached.

    The point comes from ``metrics.net_incremental_recovery`` rather than from a
    subtraction here, so §13's formula has exactly one implementation. The interval
    is then added by re-validating the dumped model: ``model_copy`` in Pydantic v2
    does **not** re-run validators, so it would happily produce a half-interval or
    an inverted one -- the same reason ``case_machine`` rebuilds instead of copying.
    """
    point_only = net_incremental_recovery(
        treatment=treatment_score.outcome,
        control=control_score.outcome,
        total_at_risk=total_at_risk,
    )
    delta_low, delta_high, p = _bootstrap(
        treatment, control, resamples=resamples, seed=seed
    )
    data = point_only.model_dump()
    data.update(
        ci_low=total_at_risk * delta_low,
        ci_high=total_at_risk * delta_high,
        resamples=resamples,
        p_value=p,
    )
    return Comparison(
        label=label,
        estimate=IncrementalRecoveryEstimate.model_validate(data),
        treatment_case_count=treatment_score.case_count,
        control_case_count=control_score.case_count,
    )


def build_scoreboard(
    outcomes: Sequence[SimulatedOutcome],
    *,
    basis: Basis = Basis.ALL_RANDOMISED,
    resamples: int = DEFAULT_RESAMPLES,
    bootstrap_seed: int = BOOTSTRAP_SEED,
) -> Scoreboard:
    """Group resolved outcomes by arm and compute §12.1's headline with a CI.

    Raises ``ValueError`` when a scoreboard arm holds no cases: an arm with no
    at-risk money has no rate (JC-33), and differencing against a fabricated zero
    would invent lift.
    """
    if resamples < 2:
        raise ValueError("a bootstrap needs at least 2 resamples")

    dropped = sorted(
        {o.arm.value for o in outcomes if o.arm not in SCOREBOARD_ARMS}
    )
    in_scope = [o for o in outcomes if o.arm in SCOREBOARD_ARMS]
    if basis is Basis.RESOLVED_ONLY:
        in_scope = [o for o in in_scope if o.was_simulated]

    grouped: dict[Arm, list[SimulatedOutcome]] = {
        arm: [o for o in in_scope if o.arm is arm] for arm in SCOREBOARD_ARMS
    }
    empty = [arm.value for arm in SCOREBOARD_ARMS if not grouped[arm]]
    if empty:
        raise ValueError(
            f"arm(s) {', '.join(empty)} hold no cases on the "
            f"{basis.value} basis; an empty arm has no recovery rate and cannot be "
            "differenced (JC-33)"
        )

    scores = {arm: _score(arm, grouped[arm]) for arm in SCOREBOARD_ARMS}
    total_at_risk = money_sum([s.total_at_risk for s in scores.values()])
    case_count = sum(s.case_count for s in scores.values())

    comparisons = (
        _compare(
            "A4 vs A0 -- the agent against doing nothing (§17's opening framing)",
            grouped[Arm.A4], grouped[Arm.A0], scores[Arm.A4], scores[Arm.A0],
            total_at_risk=total_at_risk, resamples=resamples, seed=bootstrap_seed,
        ),
        _compare(
            f"A4 vs {HEADLINE_CONTROL_ARM.value} -- §12.1's reported control arm",
            grouped[Arm.A4], grouped[HEADLINE_CONTROL_ARM],
            scores[Arm.A4], scores[HEADLINE_CONTROL_ARM],
            total_at_risk=total_at_risk, resamples=resamples, seed=bootstrap_seed,
        ),
    )

    return Scoreboard(
        basis=basis,
        arms=scores,
        comparisons=comparisons,
        total_at_risk=total_at_risk,
        case_count=case_count,
        resamples=resamples,
        bootstrap_seed=bootstrap_seed,
        warnings=_warnings(basis, scores, grouped, resamples, dropped),
    )


def _warnings(
    basis: Basis,
    scores: Mapping[Arm, ArmScore],
    grouped: Mapping[Arm, list[SimulatedOutcome]],
    resamples: int,
    dropped: Sequence[str],
) -> tuple[str, ...]:
    """Everything a reader must be told before quoting a number off this board.

    Assembled as data rather than printed inline so a caller that renders its own
    view cannot drop the caveats and keep the figure.
    """
    notes: list[str] = [
        "GROSS, not net: cost to collect is not modelled at all (no PSP fee, "
        "channel, LLM or human-minute inputs exist), so §13's net incremental "
        "recovery is computed with a zero cost term. Real costs can only lower it, "
        "and unequally -- A1 sends four times A4's contacts, A4 spends approval "
        "minutes A1 does not.",
        "POOLED, not stratum-weighted: §12.1 specifies a stratum-weighted "
        "estimator, but almost no amount-band x failure-class x segment cell is "
        "populated in both arms at this batch size, so every estimate here has "
        "strata_count=1.",
        "SIMULATED environment: " + ANCHOR_HONESTY_NOTE,
    ]

    if resamples != BOOTSTRAP_RESAMPLES:
        notes.append(
            f"{resamples} bootstrap resamples, not §12.1's {BOOTSTRAP_RESAMPLES}: "
            "the interval is a demo figure and the frozen contract reports "
            "is_publishable_as_headline = False for it."
        )

    if basis is Basis.RESOLVED_ONLY:
        notes.append(
            "PER-PROTOCOL basis: cases the agent never acted on were dropped from "
            "the denominator. This is not §12.1's unit -- randomisation only "
            "protects the intent-to-treat comparison, and the resolved subset is "
            "exactly the cases the router had a verb for, so it is biased easy."
        )
    else:
        unresolved = {
            arm.value: scores[arm].case_count - scores[arm].resolved_case_count
            for arm in SCOREBOARD_ARMS
        }
        if any(unresolved.values()):
            listed = ", ".join(f"{a}: {n}" for a, n in unresolved.items() if n)
            notes.append(
                f"INTENT-TO-TREAT basis: unresolved cases score zero recovered "
                f"({listed}). That is the correct denominator for a randomised "
                "comparison but the wrong floor for these cases: natural recovery "
                "does not switch off because the agent escalated or was denied, so "
                "the treatment arm is understated by whatever those cases would "
                "have recovered on their own."
            )

    thin = [
        f"{arm.value} ({scores[arm].case_count})"
        for arm in SCOREBOARD_ARMS
        if scores[arm].case_count < MIN_CASES_FOR_INFERENCE
    ]
    if thin:
        notes.append(
            f"THIN ARMS below {MIN_CASES_FOR_INFERENCE} cases: {', '.join(thin)}. "
            "The interval and p-value on any comparison touching them are "
            "arithmetic, not inference."
        )

    if dropped:
        notes.append(
            f"Arms outside this scoreboard's scope were dropped: "
            f"{', '.join(dropped)} (§18.4's T-12h cut keeps A0/A1/A4)."
        )

    return tuple(notes)


# ---------------------------------------------------------------------------
# Rendering, in the shape §17's opening beat reads
# ---------------------------------------------------------------------------


def _rupees(amount: Money) -> str:
    return f"{amount.rupees:>16,.2f}"


def _lakh(amount: Money) -> str:
    """§17 quotes money in lakh ("₹31.4 L at risk"). Sign kept in front."""
    value = amount.rupees / Decimal(100_000)
    return f"-₹{abs(value):,.2f} L" if value < 0 else f"₹{value:,.2f} L"


def _rate(value: Decimal | None) -> str:
    return "     -" if value is None else f"{value:>6.3f}"


_BASIS_TEXT: Final[Mapping[Basis, str]] = {
    Basis.ALL_RANDOMISED: "every randomised case (§12.1's unit: the obligation-case)",
    Basis.RESOLVED_ONLY: "resolved cases only (per-protocol, NOT §12.1's unit)",
}


def _render(board: Scoreboard) -> str:
    width = 94
    lines: list[str] = [
        "=" * width,
        "RECLAIM -- SCOREBOARD".ljust(60) + "SIMULATED ENVIRONMENT".rjust(34),
        "=" * width,
        f"{board.case_count} at-risk cases randomised across A0/A1/A4.  "
        f"{_lakh(board.total_at_risk)} at risk.",
        f"Basis: {_BASIS_TEXT[board.basis]}.",
        f"Bootstrap: {board.resamples} resamples, seed {board.bootstrap_seed}; "
        f"anchors {ANCHORS_VERSION}, sim salt {SIM_SALT!r}.",
        "",
        f"  {'arm':<42}{'cases':>7}{'recov':>7}"
        f"{'at risk (Rs)':>17}{'recovered (Rs)':>17}{'rate':>7}{'per Re':>8}",
        "  " + "-" * (width - 4),
    ]

    for arm in SCOREBOARD_ARMS:
        score = board.arms[arm]
        label = f"{arm.value}  {_ARM_LABELS[arm]}"
        lines.append(
            f"  {label:<42}{score.case_count:>7}{score.recovered_case_count:>7}"
            f"{_rupees(score.total_at_risk):>17}{_rupees(score.gross_recovered):>17}"
            f"{_rate(score.recovery_rate):>7}"
            f"{_rate(score.recovered_per_rupee_at_risk):>8}"
        )

    lines.append("")
    for comparison in board.comparisons:
        estimate = comparison.estimate
        # JC-36 as a display rule: the point and the interval are one string, so
        # there is no way to cut the line and keep the number.
        mark = (
            ""
            if comparison.meets_plan_resample_count
            else f"   [NOT PUBLISHABLE as the headline: {board.resamples} resamples "
            f"< §12.1's {BOOTSTRAP_RESAMPLES}]"
        )
        lines.append(f"  {comparison.label}")
        lines.append(
            f"    net incremental recovery = {_lakh(estimate.point)}"
            f"  (95% CI {_lakh(estimate.ci_low)} .. {_lakh(estimate.ci_high)}),"
            f"  p = {estimate.p_value},"
            f"  delta/Re = {estimate.per_rupee_delta}{mark}"
        )
        lines.append(
            f"    n = {comparison.treatment_case_count} treatment / "
            f"{comparison.control_case_count} control; "
            f"strata = {estimate.strata_count} (pooled)"
        )

    lines += ["", "  WHAT WE ARE NOT CLAIMING", "  " + "-" * (width - 4)]
    for note in board.warnings:
        first = True
        for chunk in _wrap(note, width - 8):
            lines.append(("    - " if first else "      ") + chunk)
            first = False
    lines.append("=" * width)
    return "\n".join(lines)


def _wrap(text: str, limit: int) -> list[str]:
    """A three-line word wrapper. ``textwrap`` would do, but the contracts layer's
    no-stdlib-surprises habit is worth keeping in the module a judge reads."""
    out: list[str] = []
    current = ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > limit:
            out.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(current)
    return out
