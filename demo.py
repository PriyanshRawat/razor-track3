"""One command that runs the whole agent and prints what it did: ``python demo.py``.

This module owns no logic. It seeds a batch, runs the flow over it, resolves the
simulated payer responses, computes §12.1's headline, verifies the audit chain
(§15) and checks §14.6's runtime invariants -- calling the same public functions
the tests call, in the same order, against one database. Nothing here recomputes a
number that a module already computes; if a figure below disagrees with
``metrics.py`` that is a bug in this file, not a second opinion.

Why this file is not inside ``reclaim/``
----------------------------------------
It is the one place that imports both the agent and the simulator, and §12.5.4
item 4 forbids exactly that pairing anywhere on an agent code path: the moment
``flow`` or a detector can read ``sim.anchors``, the agent can condition on the
hidden outcome table and the experiment quietly measures itself.
``test_no_agent_code_path_imports_the_simulator`` enforces that by walking every
file under ``reclaim/``, and the right answer to it is not an exemption for this
file -- an allowlist is a hole that later grows entries. A runner that drives both
sides is not agent code, and keeping it out of the package makes that a fact about
the import graph rather than a claim in a comment. A test below pins the location.

Three things it does own, each because of a specific way a demo goes wrong:

**It forces UTF-8 on its own stdout.** Every number in this report is money, and
``Money.__str__`` emits a rupee sign. Windows defaults to ``cp1252``, which raises
``UnicodeEncodeError`` on it (CONTRACTS.md Q9). Requiring a judge to set
``PYTHONIOENCODING`` before the demo works is a crash for reasons that have
nothing to do with the work, so the reconfiguration happens here.

**Its exit code is its verdict.** A broken chain or a violated invariant exits
non-zero. The subtlety is that ``InvariantReport.batch_passes`` is False on
*every* run -- five of §14.6's ten describe state this schema does not persist --
so the gate reads ``violations``, not ``batch_passes``. Unverifiable is not a
failure; a breach is. Wiring the gate to ``batch_passes`` would make the demo
permanently red and train its reader to ignore it.

**It prints the caveats next to the numbers.** ``Scoreboard.warnings`` and the
unverifiable invariant count are not appendices. The headline here is *negative*
-- the agent currently loses to a static drip, for reasons the README states --
and a runner that printed only the estimate would be the more flattering and the
less useful tool.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from typing import Sequence

from reclaim import flow, invariants
from reclaim.contracts.enums import Arm
from reclaim.sim import outcomes as sim_outcomes, scoreboard as sb
from reclaim.spine import seed, verify
from reclaim.spine.db import create_all, get_engine

__all__ = ["STAGES", "main", "run_demo"]

#: The six stage headers, in order. A test asserts every one reaches stdout, so
#: this tuple is the demo's contract with its own output rather than a decoration.
STAGES: tuple[str, ...] = (
    "[1/6] Seeding the batch",
    "[2/6] Running the agent",
    "[3/6] Simulating the payer response",
    "[4/6] Scoreboard",
    "[5/6] Verifying the audit chain",
    "[6/6] Checking the runtime invariants",
)

_RULE = "=" * 78
_THIN = "-" * 78


def _force_utf8_stdout() -> None:
    """Make the rupee sign printable. See the module docstring.

    Guarded twice: ``reconfigure`` is missing on older text wrappers and on
    pytest's capture object, and neither is a reason to fail. Under capture the
    encoding is already UTF-8, so there is nothing to fix.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - already detached
                pass


def _heading(title: str) -> None:
    print()
    print(_RULE)
    print(title)
    print(_RULE)


def run_demo(
    *,
    cases: int,
    resamples: int,
    allocation: str,
    database_url: str,
    basis: sb.Basis,
) -> int:
    """Run every stage against one database and return the process exit code."""
    engine = get_engine(database_url)
    create_all(engine)

    try:
        with engine.begin() as conn:
            _heading(STAGES[0])
            spec = seed.make_eval_spec() if allocation == "eval" else None
            batch = seed.generate(conn, n=cases, spec=spec)
            _report_seed(batch, allocation)

            _heading(STAGES[1])
            results = flow.run(conn, batch)
            _report_flow(results)

            _heading(STAGES[2])
            resolution = sim_outcomes.resolve_batch(conn, batch)
            _report_sim(resolution)

            _heading(STAGES[3])
            board = sb.build_scoreboard(
                resolution.outcomes, basis=basis, resamples=resamples
            )
            print(board.render())

            _heading(STAGES[4])
            chain = verify.verify_database_chain(conn)
            print(verify.render_report(chain, database_url=database_url))

            _heading(STAGES[5])
            report = invariants.check_all(conn)
            _report_invariants(report)

            return _verdict(chain, report)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Stage reports
# ---------------------------------------------------------------------------


def _report_seed(batch, allocation: str) -> None:
    by_arm = Counter(case.arm for case in batch)
    total = sum((case.amount_at_risk for case in batch[1:]), batch[0].amount_at_risk)
    print(f"{len(batch)} obligation-cases, {total} at risk in total.")
    print(f"Allocation: {allocation}. Arm assignment is hashlib over the frozen salt,")
    print("never the builtin hash(), so a re-run reproduces the same split exactly.")
    print()
    for arm in sorted(by_arm, key=lambda a: a.value):
        print(f"  {arm.value:<4} {by_arm[arm]:>5} cases")
    missing = sorted(a.value for a in Arm if a not in by_arm)
    if missing:
        print(f"  (no cases drawn into {', '.join(missing)})")


def _report_flow(results: Sequence[flow.CaseResult]) -> None:
    by_outcome = Counter(r.outcome for r in results)
    hedged = sum(1 for r in results if r.hedged)
    print("What the agent decided, per case:")
    print()
    for outcome, count in sorted(by_outcome.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>5}  {outcome.value}")

    skipped = (
        flow.Outcome.CONTROL_ARM_NO_ACTION,
        flow.Outcome.SIMULATION_ONLY_ARM_SKIPPED,
        flow.Outcome.BASELINE_LADDER_NOT_IMPLEMENTED,
    )
    routed = sum(n for outcome, n in by_outcome.items() if outcome not in skipped)
    acted = by_outcome.get(flow.Outcome.ALLOWED, 0)
    hedged_and_sent = sum(1 for r in results if r.hedged and r.is_allowed)
    if routed:
        print()
        print(
            f"Of the {routed} cases the agent actually routed, {acted} were auto-allowed "
            f"({acted / routed:.1%})."
        )
        print(
            f"{hedged} were contested diagnoses that took a hedged contact -- the "
            "least-committal message"
        )
        print(
            "true under every candidate root cause, discounted by the simulator. The "
            "compliance gates"
        )
        print(
            f"then allowed {hedged_and_sent} of those {hedged}; a hedge earns no exemption "
            "from §14.1."
        )

    denials = Counter(
        r.deciding_rule_id for r in results if r.outcome is flow.Outcome.DENIED
    )
    if denials:
        print()
        print("Compliance denials, by the rule that decided them (§14.1):")
        for rule_id, count in denials.most_common():
            print(f"  {count:>5}  {rule_id}")


def _report_sim(resolution) -> None:
    print("Recovery outcomes are drawn from sim/anchors.py, not observed. Every draw is")
    print("sha256 over a frozen salt, the case id and the arm, so one case cannot carry")
    print("luck from one arm into another.")
    print()
    header = f"  {'arm':<5} {'cases':>6} {'simulated':>10} {'recovered':>10}  gross recovered"
    print(header)
    for arm in sorted(resolution.by_arm, key=lambda a: a.value):
        tally = resolution.by_arm[arm]
        print(
            f"  {arm.value:<5} {tally.case_count:>6} {tally.simulated_case_count:>10} "
            f"{tally.recovered_case_count:>10}  {tally.gross_recovered}"
        )


def _report_invariants(report: invariants.InvariantReport) -> None:
    for result in report:
        print(
            f"  #{result.number:<3} {result.status.value:<14} "
            f"n={result.candidates_examined}"
        )
        print(f"        {result.text}")
        if result.detail:
            print(f"        -> {result.detail[:300]}")
        print()

    holds = report.counts().get(invariants.InvariantStatus.HOLDS, 0)
    print(_THIN)
    print(f"  {report.summary()}")
    print(
        f"  {holds} genuinely hold. {len(report.unverifiable)} are not_checkable or "
        "vacuous, and those are NOT a pass:"
    )
    print(
        "  the state they describe (consent store, holds table, mandate caps, "
        "notification log)"
    )
    print(
        "  has no home in this schema, so no amount of data turns them green. Only a "
        "VIOLATED"
    )
    print("  result fails this run.")


def _verdict(chain, report: invariants.InvariantReport) -> int:
    violations = report.violations
    ok = chain.verified and not violations
    breached = ", ".join(f"#{v.number}" for v in violations)

    _heading("VERDICT")
    print(f"  audit chain      : {'VERIFIED' if chain.verified else 'FAILED'}")
    print(f"  invariants       : {'no violations' if ok else 'VIOLATED ' + breached}")
    print()
    print(f"  {'PASS' if ok else 'FAIL'}")
    print("  README.md states what these numbers do and do not show.")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python demo.py",
        description="Run RECLAIM end to end over a seeded batch and print the report.",
    )
    parser.add_argument(
        "--cases", type=int, default=200, help="how many obligation-cases to seed"
    )
    parser.add_argument(
        "--resamples",
        type=int,
        default=sb.DEFAULT_RESAMPLES,
        help=(
            "bootstrap resamples for the confidence interval; §12.1 requires "
            f"{sb.DEFAULT_RESAMPLES}, and anything less is reported as not publishable"
        ),
    )
    parser.add_argument(
        "--allocation",
        choices=("planned", "eval"),
        default="eval",
        help=(
            "'planned' draws into all six arms; 'eval' is §18.4's T-12h cut, which "
            "keeps A0/A1/A4 and switches the rest off explicitly at 0 permille"
        ),
    )
    parser.add_argument(
        "--basis",
        choices=[b.value for b in sb.Basis],
        default=sb.Basis.ALL_RANDOMISED.value,
        help=(
            "all_randomised is intent-to-treat (§12.1's unit); resolved_only is "
            "per-protocol and flatters the agent"
        ),
    )
    parser.add_argument(
        "--database-url",
        default="sqlite://",
        help="SQLAlchemy URL; the default is a throwaway in-memory database",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_stdout()
    parser = _parser()
    args = parser.parse_args(argv)
    if args.cases < 1:
        parser.error("--cases must be at least 1")
    return run_demo(
        cases=args.cases,
        resamples=args.resamples,
        allocation=args.allocation,
        database_url=args.database_url,
        basis=sb.Basis(args.basis),
    )


if __name__ == "__main__":  # pragma: no cover - exercised via main() in tests
    raise SystemExit(main())
