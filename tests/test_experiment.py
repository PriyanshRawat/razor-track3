"""Contract tests for the deterministic arm assignment (deliverable #7).

§12.1, verbatim: "Assignment: deterministic hash of ``case_id + experiment_salt``
-> arm. Stratified by amount band x failure class x segment. Logged at creation,
immutable."

Two things make this file worth more than its size. First, the assignment recipe is
pinned *independently* here -- the test recomputes the hash from the documented
recipe instead of asking the module what it thinks the answer is, so a refactor that
silently re-randomises 2,000 cases fails. Second, the cross-process test proves the
implementation uses ``hashlib`` and not the builtin ``hash()``, whose salt changes
every interpreter start and would make the experiment unreproducible in the exact
way §12.5 promises it is not.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from math import gcd
from pathlib import Path

import pytest
from pydantic import ValidationError

from reclaim.contracts.enums import Arm, RiskClass, Segment
from reclaim.contracts.experiment import (
    ARM_ORDER,
    PERMILLE_TOTAL,
    PLANNED_ARM_WEIGHTS_PERMILLE,
    ArmAssignment,
    AssignmentMethod,
    ExperimentSpec,
    assign_arm,
    assign_arm_blocked,
    assignment_permille,
    verify_assignment,
)
from reclaim.contracts.metrics import MetricKey
from reclaim.contracts.money import Money
from reclaim.contracts.strata import StratumKey

_T0 = datetime(2026, 3, 1, 4, 30, tzinfo=timezone.utc)
_SALT = "reclaim-2026-03-01-preregistered"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _spec(**kw) -> ExperimentSpec:
    kwargs = dict(
        experiment_id="exp_headline_1",
        experiment_salt=_SALT,
        arm_weights_permille=PLANNED_ARM_WEIGHTS_PERMILLE,
        control_arm=Arm.A1,
        treatment_arm=Arm.A4,
        planned_case_count=2000,
        stopping_rule="Fixed n = 2,000 cases. No interim looks; no early stop.",
        registered_at=_T0,
    )
    kwargs.update(kw)
    return ExperimentSpec(**kwargs)


def _stratum(amount: int = 1499, segment: Segment = Segment.B2C_STANDARD) -> StratumKey:
    return StratumKey.build(
        amount=Money.from_rupees(amount),
        failure_class=RiskClass.FAILED_RECURRING_DEBIT,
        segment=segment,
    )


# ------------------------------------------------------------ the spec itself


def test_the_planned_weights_sum_to_one_thousand_permille():
    assert sum(PLANNED_ARM_WEIGHTS_PERMILLE.values()) == PERMILLE_TOTAL == 1000


def test_the_two_headline_arms_carry_the_largest_shares():
    """§12.1: control (A1) + treatment (A4), 'plus the ablation arms below on a
    smaller share'. The headline CI is only as narrow as these two arms are big."""
    weights = PLANNED_ARM_WEIGHTS_PERMILLE
    assert weights[Arm.A1] == weights[Arm.A4]
    for arm in (Arm.A0, Arm.A2, Arm.A3, Arm.A5):
        assert weights[arm] < weights[Arm.A1]


def test_weights_that_do_not_sum_to_one_thousand_are_refused():
    """999 permille means one case in a thousand has no arm. Silently normalising
    would change every share by a hair and make the run irreproducible."""
    bad = dict(PLANNED_ARM_WEIGHTS_PERMILLE)
    bad[Arm.A0] = bad[Arm.A0] - 1
    with pytest.raises(ValidationError):
        _spec(arm_weights_permille=bad)


def test_every_arm_must_appear_in_the_weight_table():
    """An arm dropped by omission is invisible; an arm dropped by a zero weight is
    a stated decision. Only the second is allowed."""
    partial = {a: w for a, w in PLANNED_ARM_WEIGHTS_PERMILLE.items() if a is not Arm.A5}
    partial[Arm.A4] = partial[Arm.A4] + PLANNED_ARM_WEIGHTS_PERMILLE[Arm.A5]
    with pytest.raises(ValidationError):
        _spec(arm_weights_permille=partial)


def test_an_arm_may_be_switched_off_with_a_zero_weight():
    weights = dict(PLANNED_ARM_WEIGHTS_PERMILLE)
    weights[Arm.A4] = weights[Arm.A4] + weights[Arm.A5]
    weights[Arm.A5] = 0
    spec = _spec(arm_weights_permille=weights)
    assert spec.arm_weights_permille[Arm.A5] == 0


def test_the_control_and_treatment_arms_must_differ():
    with pytest.raises(ValidationError):
        _spec(control_arm=Arm.A4, treatment_arm=Arm.A4)


def test_a_headline_arm_with_no_share_is_refused():
    """Declaring A5 the treatment while giving it zero weight would produce an
    empty comparison that only shows up as a division by zero on scoreboard day."""
    weights = dict(PLANNED_ARM_WEIGHTS_PERMILLE)
    weights[Arm.A4] = weights[Arm.A4] + weights[Arm.A5]
    weights[Arm.A5] = 0
    with pytest.raises(ValidationError):
        _spec(arm_weights_permille=weights, treatment_arm=Arm.A5)


def test_the_primary_metric_must_be_the_headline_metric():
    assert _spec().primary_metric is MetricKey.NET_INCREMENTAL_RECOVERY
    with pytest.raises(ValidationError):
        _spec(primary_metric=MetricKey.RECOVERY_RATE)


def test_the_recovery_windows_are_the_plan_s_twenty_one_and_forty_five_days():
    spec = _spec()
    assert spec.b2c_recovery_window_days == 21
    assert spec.b2b_recovery_window_days == 45


def test_the_recovery_window_follows_the_segment():
    spec = _spec()
    assert spec.recovery_window_days(Segment.B2C_STANDARD) == 21
    assert spec.recovery_window_days(Segment.B2C_PREMIUM) == 21
    assert spec.recovery_window_days(Segment.B2B_SMB) == 45
    assert spec.recovery_window_days(Segment.B2B_STRATEGIC) == 45


def test_a_short_or_punctuated_salt_is_refused():
    """The salt is concatenated with a separator; a salt containing the separator
    would make two different (salt, case) pairs hash identically."""
    with pytest.raises(ValidationError):
        _spec(experiment_salt="short")
    with pytest.raises(ValidationError):
        _spec(experiment_salt="has|a|separator")


def test_the_spec_is_frozen_after_registration():
    spec = _spec()
    with pytest.raises(ValidationError):
        spec.experiment_salt = "another-salt-entirely"


def test_an_untyped_experiment_id_is_refused():
    with pytest.raises(ValidationError):
        _spec(experiment_id="headline_1")


def test_a_stopping_rule_is_mandatory():
    """§12.5: the stopping rule is pre-registered. An unstated one is a licence to
    stop when the number looks good."""
    with pytest.raises(ValidationError):
        _spec(stopping_rule="")


# --------------------------------------------------------- pre-registration


def test_the_preregistration_digest_pins_the_whole_spec():
    assert len(_spec().preregistration_digest) == 64
    assert _spec().preregistration_digest == _spec().preregistration_digest


def test_changing_any_pre_registered_field_changes_the_digest():
    base = _spec().preregistration_digest
    assert _spec(planned_case_count=2001).preregistration_digest != base
    assert _spec(experiment_salt=_SALT + "-v2").preregistration_digest != base
    assert _spec(stopping_rule="Stop at n=2,000 or 14 days.").preregistration_digest != base
    assert _spec(b2b_recovery_window_days=44).preregistration_digest != base


def test_the_digest_is_stable_across_processes():
    """It goes in the audit chain and in the pre-registration commit, so a value
    that changes between runs would make the commit meaningless."""
    out = _run_in_subprocess(
        "print(_spec().preregistration_digest)", seeds=("0", "1", "12345")
    )
    assert len(out) == 1


# ------------------------------------------------------------- the assignment


def _expected_permille(case_id: str, salt: str = _SALT) -> int:
    """The documented recipe, recomputed here from scratch (see the module
    docstring): SHA-256 over ``salt|case_id``, top 8 bytes big-endian, mod 1000."""
    raw = hashlib.sha256(f"{salt}|{case_id}".encode("utf-8")).digest()
    return int.from_bytes(raw[:8], "big") % 1000


def _expected_arm(case_id: str, spec: ExperimentSpec) -> Arm:
    draw = _expected_permille(case_id, spec.experiment_salt)
    cumulative = 0
    for arm in ARM_ORDER:
        cumulative += spec.arm_weights_permille[arm]
        if draw < cumulative:
            return arm
    raise AssertionError("weights did not cover the draw")


def test_the_assignment_follows_the_documented_hash_recipe():
    """Pinned against an independent recomputation, not against the module."""
    spec = _spec()
    for n in range(200):
        case_id = f"case_{n:05d}"
        assert assignment_permille(case_id, spec) == _expected_permille(case_id)
        assert assign_arm(case_id, spec) is _expected_arm(case_id, spec)


def test_the_same_case_always_lands_in_the_same_arm():
    spec = _spec()
    first = assign_arm("case_00042", spec)
    assert all(assign_arm("case_00042", spec) is first for _ in range(1000))


def test_the_assignment_is_stable_across_processes():
    """The real test of 'deterministic': builtin hash() would pass every
    in-process test above and fail this one, because PYTHONHASHSEED varies."""
    out = _run_in_subprocess(
        "print([assign_arm(f'case_{n:05d}', _spec()).value for n in range(50)])",
        seeds=("0", "1", "999"),
    )
    assert len(out) == 1


def test_a_different_salt_reshuffles_most_of_the_book():
    """Re-randomisation must actually re-randomise; a salt that barely moves cases
    would let a second run inherit the first run's luck."""
    a, b = _spec(), _spec(experiment_salt=_SALT + "-v2")
    ids = [f"case_{n:05d}" for n in range(2000)]
    moved = sum(1 for cid in ids if assign_arm(cid, a) is not assign_arm(cid, b))
    assert moved > 1000  # ~74% expected under these weights


def test_observed_shares_match_the_declared_weights():
    """20,000 draws. Each share's standard error is under 0.4pp here, so a 1pp
    tolerance is ~3 sigma: tight enough to catch a mis-indexed cumulative range,
    loose enough not to flake."""
    spec = _spec()
    counts = Counter(assign_arm(f"case_{n:06d}", spec) for n in range(20_000))
    for arm, permille in spec.arm_weights_permille.items():
        share = Decimal(counts[arm]) / Decimal(20_000)
        expected = Decimal(permille) / Decimal(1000)
        assert abs(share - expected) < Decimal("0.01"), f"{arm}: {share} vs {expected}"


def test_a_zero_weight_arm_never_receives_a_case():
    weights = dict(PLANNED_ARM_WEIGHTS_PERMILLE)
    weights[Arm.A4] = weights[Arm.A4] + weights[Arm.A5]
    weights[Arm.A5] = 0
    spec = _spec(arm_weights_permille=weights)
    assigned = {assign_arm(f"case_{n:05d}", spec) for n in range(5000)}
    assert Arm.A5 not in assigned


def test_a_single_arm_at_full_weight_takes_everything():
    """The boundary case of the cumulative walk: no draw may fall through it."""
    weights = {arm: 0 for arm in ARM_ORDER}
    weights[Arm.A1] = 1000
    # A single non-empty arm cannot host a comparison, so the spec refuses it ...
    with pytest.raises(ValidationError):
        _spec(arm_weights_permille=weights, control_arm=Arm.A1, treatment_arm=Arm.A0)
    # ... and with a legal pair of non-empty arms, the walk still never falls off:
    weights[Arm.A1] = 999
    weights[Arm.A4] = 1
    spec = _spec(arm_weights_permille=weights)
    assert {assign_arm(f"case_{n:05d}", spec) for n in range(500)} <= {Arm.A1, Arm.A4}


def test_an_untyped_case_id_is_refused_by_the_assigner():
    with pytest.raises(ValueError):
        assign_arm("00042", _spec())


# ------------------------------------------------- stratified block assignment


def test_the_block_size_is_the_smallest_that_preserves_the_ratios():
    spec = _spec()
    divisor = gcd(*spec.arm_weights_permille.values())
    assert spec.block_size == PERMILLE_TOTAL // divisor
    assert spec.block_size == 50  # 80:320:100:100:320:80 -> 4:16:5:5:16:4


def test_one_complete_block_is_exactly_balanced():
    """This is the whole point of the blocked variant: within a stratum, after 50
    cases the arm counts are exact, not binomial."""
    spec = _spec()
    stratum = _stratum()
    arms = Counter(
        assign_arm_blocked(f"case_{n:05d}", spec, stratum=stratum, within_stratum_rank=n)
        for n in range(spec.block_size)
    )
    divisor = gcd(*spec.arm_weights_permille.values())
    for arm, permille in spec.arm_weights_permille.items():
        assert arms[arm] == permille // divisor


def test_blocked_assignment_is_deterministic_in_the_rank():
    spec, stratum = _spec(), _stratum()
    first = assign_arm_blocked("case_1", spec, stratum=stratum, within_stratum_rank=7)
    for _ in range(100):
        assert (
            assign_arm_blocked("case_1", spec, stratum=stratum, within_stratum_rank=7)
            is first
        )


def test_blocked_assignment_ignores_the_case_id():
    """It is positional by construction. Stating it as a test stops someone from
    'fixing' it later by mixing the case id back into the block permutation, which
    would destroy the exact balance."""
    spec, stratum = _spec(), _stratum()
    assert assign_arm_blocked(
        "case_aaa", spec, stratum=stratum, within_stratum_rank=3
    ) is assign_arm_blocked("case_bbb", spec, stratum=stratum, within_stratum_rank=3)


def test_each_stratum_gets_its_own_permutation():
    """A shared permutation would align every stratum's arm order, so a
    time-varying environment would hit the same arms in the same order everywhere."""
    spec = _spec()
    a, b = _stratum(1499), _stratum(500000)
    sequence_a = [
        assign_arm_blocked("case_1", spec, stratum=a, within_stratum_rank=r)
        for r in range(spec.block_size)
    ]
    sequence_b = [
        assign_arm_blocked("case_1", spec, stratum=b, within_stratum_rank=r)
        for r in range(spec.block_size)
    ]
    assert sequence_a != sequence_b


def test_consecutive_blocks_are_permuted_differently():
    spec, stratum = _spec(), _stratum()
    block_0 = [
        assign_arm_blocked("case_1", spec, stratum=stratum, within_stratum_rank=r)
        for r in range(spec.block_size)
    ]
    block_1 = [
        assign_arm_blocked("case_1", spec, stratum=stratum, within_stratum_rank=r)
        for r in range(spec.block_size, 2 * spec.block_size)
    ]
    assert block_0 != block_1
    assert Counter(block_0) == Counter(block_1)  # ... but equally balanced


def test_a_negative_rank_is_refused():
    with pytest.raises(ValueError):
        assign_arm_blocked("case_1", _spec(), stratum=_stratum(), within_stratum_rank=-1)


def test_blocked_assignment_is_stable_across_processes():
    out = _run_in_subprocess(
        "print([assign_arm_blocked('case_1', _spec(), stratum=_stratum(), "
        "within_stratum_rank=r).value for r in range(50)])",
        seeds=("0", "1", "999"),
    )
    assert len(out) == 1


# ------------------------------------------------------------- the record


def _assignment(**kw) -> ArmAssignment:
    spec = kw.pop("spec", _spec())
    kwargs = dict(
        case_id="case_00042",
        experiment_id=spec.experiment_id,
        arm=assign_arm("case_00042", spec),
        stratum=_stratum(),
        method=AssignmentMethod.HASH_INDEPENDENT,
        draw_permille=assignment_permille("case_00042", spec),
        salt_digest=spec.salt_digest,
        assigned_at=_T0,
    )
    kwargs.update(kw)
    return ArmAssignment(**kwargs)


def test_the_assignment_record_is_immutable():
    """§12.1: 'Logged at creation, immutable.'"""
    record = _assignment()
    with pytest.raises(ValidationError):
        record.arm = Arm.A0


def test_the_record_carries_the_salt_digest_not_the_salt():
    """Publishing the salt at pre-registration is required for reproducibility;
    copying it onto every one of 2,000 rows is not, and an audit log is the wrong
    place to keep the thing that predicts future assignments."""
    record = _assignment()
    assert record.salt_digest == hashlib.sha256(_SALT.encode("utf-8")).hexdigest()
    assert _SALT not in record.model_dump_json()


def test_verify_assignment_accepts_a_faithful_record():
    spec = _spec()
    assert verify_assignment(_assignment(spec=spec), spec).is_valid is True


def test_verify_assignment_catches_a_moved_case():
    """The attack this defends against is not malice, it is a re-run: someone
    re-randomises, forgets, and compares arms that were assigned under two
    different salts."""
    spec = _spec()
    wrong_arm = next(a for a in ARM_ORDER if a is not assign_arm("case_00042", spec))
    result = verify_assignment(_assignment(spec=spec, arm=wrong_arm), spec)
    assert result.is_valid is False
    assert "arm" in result.reason


def test_verify_assignment_catches_a_record_from_a_different_salt():
    spec = _spec()
    other = _spec(experiment_salt=_SALT + "-v2")
    result = verify_assignment(_assignment(spec=other), spec)
    assert result.is_valid is False
    assert "salt" in result.reason


def test_verify_assignment_catches_a_record_from_a_different_experiment():
    spec = _spec()
    record = _assignment(spec=spec, experiment_id="exp_other_run")
    assert verify_assignment(record, spec).is_valid is False


def test_verify_assignment_checks_the_blocked_method_too():
    spec, stratum = _spec(), _stratum()
    arm = assign_arm_blocked("case_00042", spec, stratum=stratum, within_stratum_rank=3)
    record = _assignment(
        spec=spec,
        arm=arm,
        method=AssignmentMethod.PERMUTED_BLOCK,
        within_stratum_rank=3,
        draw_permille=None,
    )
    assert verify_assignment(record, spec).is_valid is True
    moved = _assignment(
        spec=spec,
        arm=arm,
        method=AssignmentMethod.PERMUTED_BLOCK,
        within_stratum_rank=4,
        draw_permille=None,
    )
    if assign_arm_blocked(
        "case_00042", spec, stratum=stratum, within_stratum_rank=4
    ) is not arm:
        assert verify_assignment(moved, spec).is_valid is False


def test_a_blocked_record_must_carry_its_rank():
    with pytest.raises(ValidationError):
        _assignment(method=AssignmentMethod.PERMUTED_BLOCK, draw_permille=None)


def test_a_hash_record_must_carry_its_draw():
    with pytest.raises(ValidationError):
        _assignment(draw_permille=None)


def test_a_hash_record_may_not_claim_a_block_rank():
    """Two methods, two provenances. A row carrying both cannot be re-verified
    unambiguously."""
    with pytest.raises(ValidationError):
        _assignment(within_stratum_rank=3)


def test_the_record_is_canonically_serialisable():
    from reclaim.contracts.canonical import canonical_json

    canonical_json(_assignment().model_dump(mode="json"))


# ----------------------------------------------------------------- helpers


def _run_in_subprocess(body: str, seeds: tuple[str, ...]) -> set[str]:
    """Run ``body`` under several PYTHONHASHSEED values and return the distinct
    stdout values. One distinct value means the result does not depend on the
    interpreter's per-process hash seed."""
    preamble = (
        "import sys; sys.path.insert(0, %r)\n"
        "from tests.test_experiment import _spec, _stratum\n"
        "from reclaim.contracts.experiment import assign_arm, assign_arm_blocked\n"
    ) % str(_REPO_ROOT)
    outputs: set[str] = set()
    for seed in seeds:
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8")
        completed = subprocess.run(
            [sys.executable, "-c", preamble + body],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(_REPO_ROOT),
        )
        assert completed.returncode == 0, completed.stderr
        outputs.add(completed.stdout.strip())
    return outputs
