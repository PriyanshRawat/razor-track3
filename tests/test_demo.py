"""The one command a judge runs, and the two ways it has historically broken.

``demo.py`` is not a feature -- it is the seam where every other module has
to actually work together against one database. So the tests here are integration
tests on purpose: they run the real seed, the real flow, the real simulation, the
real bootstrap and the real checkers, and assert on what a reader would see.

Two of them exist because of specific, recorded failures rather than symmetry:
``cp1252`` (CONTRACTS.md Q9) makes printing a rupee sign a crash on this machine,
and a demo whose exit code ignores its own verdict is a demo that reports success
over a broken chain.
"""

from __future__ import annotations

import pytest

import demo
from reclaim.spine import verify


def _run(capsys, argv):
    code = demo.main(argv)
    return code, capsys.readouterr().out


def test_the_demo_runs_end_to_end_and_exits_clean(capsys):
    code, out = _run(capsys, ["--cases", "60", "--resamples", "200"])

    assert code == 0, out
    for stage in demo.STAGES:
        assert stage in out, f"missing stage: {stage}"


def test_the_demo_prints_the_rupee_sign_without_crashing(capsys):
    """CONTRACTS.md Q9: Windows ``cp1252`` raises ``UnicodeEncodeError`` on the
    rupee sign, and ``Money.__str__`` emits one. Every number in this report is
    money, so a demo that does not force UTF-8 on its own stdout crashes on a
    judge's machine for reasons that have nothing to do with the work. The module
    must not need ``PYTHONIOENCODING`` set for it."""
    _, out = _run(capsys, ["--cases", "60", "--resamples", "200"])

    assert "\u20b9" in out


def test_a_broken_audit_chain_makes_the_demo_exit_non_zero(capsys, monkeypatch):
    """The verdict has to reach the exit code.

    §15's whole claim is that the log is tamper-evident. A runner that prints
    FAILED and returns 0 hands a green CI badge to a tampered database, which is
    worse than not checking at all.
    """
    def broken(conn, **kwargs):
        return verify.ChainReport(
            verified=False,
            contract_is_valid=False,
            kind=verify.ChainFailureKind.ROW_TAMPERED,
            rows_checked=7,
            reason="injected by a test",
            first_bad_sequence=4,
        )

    monkeypatch.setattr(demo.verify, "verify_database_chain", broken)
    code, out = _run(capsys, ["--cases", "40", "--resamples", "200"])

    assert code != 0
    assert "FAIL" in out.upper()


def test_the_demo_reports_a_violated_invariant_rather_than_averaging_it_away(
    capsys, monkeypatch
):
    """An invariant breach must be visible *and* fatal.

    ``InvariantReport.batch_passes`` is False on every run by design -- five of
    §14.6's ten are structurally unverifiable against this schema -- so the exit
    code cannot read it. It reads ``violations``, and this pins that distinction:
    unverifiable is not a failure, a breach is.
    """
    from reclaim import invariants

    real = invariants.check_all

    def with_violation(conn):
        report = real(conn)
        broken = invariants.InvariantResult(
            number=1,
            text=invariants.INVARIANT_TEXT[1],
            status=invariants.InvariantStatus.VIOLATED,
            candidates_examined=1,
            offending_case_ids=("case_seed_0001",),
            detail="injected by a test",
        )
        return invariants.InvariantReport(
            (broken,) + tuple(r for r in report.results if r.number != 1)
        )

    monkeypatch.setattr(demo.invariants, "check_all", with_violation)
    code, out = _run(capsys, ["--cases", "40", "--resamples", "200"])

    assert code != 0
    assert "violated" in out.lower()


def test_a_clean_run_is_not_called_a_pass_on_the_unverifiable_invariants(capsys):
    """The softer half of the same rule.

    Five invariants can never be green here, and the demo must say so in words
    rather than printing "10/10" or quietly dropping them from the count. This is
    the line that stops the report from over-claiming.
    """
    _, out = _run(capsys, ["--cases", "60", "--resamples", "200"])

    assert "not_checkable" in out.lower()
    assert "not a pass" in out.lower()


@pytest.mark.parametrize("allocation", ["planned", "eval"])
def test_both_allocations_run(capsys, allocation):
    """§18.4's T-12h cut keeps A0/A1/A4, and ``--allocation eval`` is how that
    allocation is selected. Both must survive a full pass: the planned one draws
    cases into arms Phase 1 does not implement, which is exactly the population
    invariant #9 used to report as orphaned."""
    code, out = _run(
        capsys, ["--cases", "60", "--resamples", "200", "--allocation", allocation]
    )

    assert code == 0, out


def test_the_runner_lives_outside_the_agent_package():
    """The structural half of §12.5.4 item 4, pinned so it cannot drift back.

    ``demo.py`` imports both ``reclaim.flow`` and ``reclaim.sim``, which is the one
    pairing ``test_no_agent_code_path_imports_the_simulator`` exists to forbid
    inside ``reclaim/``. Moving this file into the package would either break that
    test or, worse, prompt someone to add an allowlist entry to it -- and an
    allowlist with one entry is an allowlist with three entries a month later. The
    separation survives because the runner is not in the package, not because the
    checker was taught to ignore it.
    """
    import pathlib

    module = pathlib.Path(demo.__file__).resolve()
    package = (pathlib.Path(__file__).resolve().parents[1] / "reclaim").resolve()

    assert package not in module.parents, (
        f"{module.name} imports the simulator and must stay outside {package.name}/"
    )
