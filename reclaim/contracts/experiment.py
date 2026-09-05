"""Deterministic, stratified arm assignment for the randomised batch (deliverable #7).

§12.1, verbatim::

    Unit: obligation-case. Assignment: deterministic hash of ``case_id +
    experiment_salt`` -> arm. Stratified by amount band x failure class x segment.
    Logged at creation, immutable.

This module is the reason the headline number is allowed to say "incremental". If
assignment is not reproducible, §12.5's live challenge -- *"change any simulator
parameter and we re-run in 90 seconds"* -- becomes a re-randomisation, and every
comparison silently changes its own baseline.

THE RECIPE, STATED SO IT CAN BE RE-IMPLEMENTED FROM THIS PARAGRAPH ALONE
------------------------------------------------------------------------
``draw = int.from_bytes(sha256(f"{salt}|{case_id}").digest()[:8], "big") % 1000``,
then walk ``ARM_ORDER`` accumulating permille weights and take the first arm whose
cumulative total exceeds ``draw``. That is all of it. The contract test recomputes
this independently rather than asking the module, so a refactor that re-randomises
2,000 cases fails loudly instead of quietly.

CONTRACT DECISION (JC-37): hashlib, never the builtin ``hash()``
----------------------------------------------------------------
Python's ``hash()`` for strings is randomised per process by ``PYTHONHASHSEED``.
Using it would produce an assignment that is stable within one run, passes every
in-process test, and silently differs between the pre-registration run and the
judge's reproduction. ``tests/test_experiment.py`` runs the assigner in
subprocesses under three different seeds and asserts one distinct answer, which is
the only way to actually pin this.

CONTRACT DECISION (JC-38): weights are integer permille, not floats
-------------------------------------------------------------------
Floating-point shares do not sum to exactly 1.0 (0.08 + 0.32 + 0.10 + 0.10 + 0.32 +
0.08 = 0.9999999999999999), so a cumulative walk over them has a boundary case
where a draw falls off the end. Integers in thousandths sum exactly, are validated
to total 1000, and serialise into the audit chain without ``canonical_json``
rejecting them (JC-15). Permille rather than percent because A0 and A5 at 8% each
would otherwise force a coarser split later.

CONTRACT DECISION (JC-39): two methods, and the record says which
-----------------------------------------------------------------
``assign_arm`` is independent hashing: each case draws on its own, so shares are
correct in expectation and only *approximately* balanced in any finite stratum.
``assign_arm_blocked`` is permuted-block assignment: within a stratum, every
consecutive block of ``block_size`` cases contains *exactly* the declared arm
counts. Blocking is what makes small strata comparable -- with 6 arms and, say, 40
cases in the ``gt_10l`` band, independent hashing routinely leaves an arm with two
cases and another with eleven, and the stratum-weighted estimator then carries a
term with almost no data behind it.

Blocking costs predictability: knowing the stratum, the rank, and the salt, the
next arm is derivable. That matters in a trial where an operator could steer
enrolment. It does not matter here -- cases arrive from a simulator, and nothing in
the pipeline consults the arm before the case exists. The trade is stated rather
than assumed, and both functions ship so Phase 1 can choose per stratum.
**Flagged for review:** which method the headline run uses is a live decision. See
CONTRACTS.md.

CONTRACT DECISION (JC-40): the record carries the salt's digest, not the salt
-----------------------------------------------------------------------------
The salt is published once, at pre-registration. Copying it onto 2,000 assignment
rows would put the value that predicts every future assignment into the operational
log, for no verification benefit: the digest is enough to prove two records came
from the same randomisation, and re-verification is done with the spec in hand.

CONTRACT DECISION (JC-41): the spec hashes itself into a pre-registration digest
--------------------------------------------------------------------------------
§12.5 requires metrics, arms, window and stopping rule committed *before* the run,
with a git timestamp. ``preregistration_digest`` is the 64 characters that pin all
of it; committing that string is the machine-checkable half of the promise, and
``verify_assignment`` will not accept a record whose salt disagrees.
"""

from __future__ import annotations

import hashlib
from math import gcd
from typing import Final, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from reclaim.contracts.canonical import digest
from reclaim.contracts.enums import Arm, Segment
from reclaim.contracts.ids import CaseId, is_valid_id
from reclaim.contracts.metrics import MetricKey
from reclaim.contracts.strata import STRATUM_DEFINITION_VERSION, StratumKey
from reclaim.contracts.temporal import UtcDatetime
from reclaim.contracts.versions import ARM_ASSIGNMENT_VERSION

try:  # pragma: no cover - 3.11+
    from enum import StrEnum
except ImportError:  # pragma: no cover - 3.10 and earlier
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass


__all__ = [
    "ARM_ORDER",
    "AssignmentMethod",
    "AssignmentVerification",
    "ArmAssignment",
    "ExperimentSpec",
    "HASH_SEPARATOR",
    "PERMILLE_TOTAL",
    "PLANNED_ARM_WEIGHTS_PERMILLE",
    "assign_arm",
    "assign_arm_blocked",
    "assignment_permille",
    "verify_assignment",
]

#: Shares are thousandths (JC-38).
PERMILLE_TOTAL: Final[int] = 1000

#: The separator between salt and case id. Forbidden inside the salt so that
#: ``("a|b", "c")`` and ``("a", "b|c")`` cannot hash to the same draw.
HASH_SEPARATOR: Final[str] = "|"

#: The order the cumulative walk visits arms in. Fixed, because changing it
#: re-randomises the whole book without changing any weight.
ARM_ORDER: Final[tuple[Arm, ...]] = (Arm.A0, Arm.A1, Arm.A2, Arm.A3, Arm.A4, Arm.A5)

#: §12.1's split: the two headline arms take 64% between them, the four ablation
#: arms share the rest. A0 and A5 are the cheapest to under-power -- A0 measures
#: natural recovery, which is a wide distribution, and A5 exists to be *reported*,
#: not to carry a tight CI -- so they take the smallest shares.
PLANNED_ARM_WEIGHTS_PERMILLE: Final[Mapping[Arm, int]] = {
    Arm.A0: 80,
    Arm.A1: 320,
    Arm.A2: 100,
    Arm.A3: 100,
    Arm.A4: 320,
    Arm.A5: 80,
}

#: §12.1: "21 days from detection for B2C, 45 for B2B."
B2C_RECOVERY_WINDOW_DAYS: Final[int] = 21
B2B_RECOVERY_WINDOW_DAYS: Final[int] = 45

_B2C_SEGMENTS: Final[frozenset[Segment]] = frozenset(
    {Segment.B2C_STANDARD, Segment.B2C_PREMIUM}
)


class AssignmentMethod(StrEnum):
    """Which of the two recipes produced a given assignment (JC-39)."""

    #: Independent per-case hashing. Correct shares in expectation.
    HASH_INDEPENDENT = "hash_independent"
    #: Permuted blocks within a stratum. Exact shares every ``block_size`` cases.
    PERMUTED_BLOCK = "permuted_block"


class ExperimentSpec(BaseModel):
    """The pre-registered configuration of one randomised batch (§12.1, §12.5).

    Frozen: every field is part of the pre-registration digest, so a spec that
    could be edited after the fact would make the digest a decoration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str = Field(
        min_length=5,
        max_length=64,
        description="Prefixed 'exp_'. Not in ids.py's registry because an "
        "experiment is not a domain object the agent ever acts on.",
    )
    experiment_salt: str = Field(
        min_length=16,
        max_length=128,
        description="Published at pre-registration. Changing it re-randomises "
        "every case, which is exactly why it is inside the digest.",
    )
    arm_weights_permille: Mapping[Arm, int] = Field(
        description="Thousandths, summing to exactly 1000 (JC-38)."
    )
    control_arm: Arm = Field(description="§12.1: A1, the realistic baseline.")
    treatment_arm: Arm = Field(description="§12.1: A4, the full system.")
    primary_metric: MetricKey = Field(
        default=MetricKey.NET_INCREMENTAL_RECOVERY,
        description="§13's headline. Pinned so a run cannot quietly promote a "
        "friendlier metric after seeing the data.",
    )
    planned_case_count: int = Field(
        gt=0, description="Pre-registered n. §12.4 quotes 2,000 cases."
    )
    stopping_rule: str = Field(
        min_length=10,
        max_length=500,
        description="§12.5 requires it in writing before the run. An unstated "
        "rule is a licence to stop when the number looks good.",
    )
    b2c_recovery_window_days: int = Field(default=B2C_RECOVERY_WINDOW_DAYS, gt=0)
    b2b_recovery_window_days: int = Field(default=B2B_RECOVERY_WINDOW_DAYS, gt=0)
    registered_at: UtcDatetime = Field(
        description="When the spec was committed. The git timestamp is the "
        "external witness; this is the internal one."
    )
    stratum_definition_version: str = STRATUM_DEFINITION_VERSION
    assignment_version: str = ARM_ASSIGNMENT_VERSION

    # -- validation -------------------------------------------------------

    @field_validator("experiment_id")
    @classmethod
    def _experiment_ids_are_typed(cls, value: str) -> str:
        if not is_valid_id(value, "exp"):
            raise ValueError(f"experiment_id {value!r} must look like 'exp_<slug>'")
        return value

    @field_validator("experiment_salt")
    @classmethod
    def _the_salt_is_hash_safe(cls, value: str) -> str:
        if HASH_SEPARATOR in value:
            raise ValueError(
                f"experiment_salt may not contain {HASH_SEPARATOR!r}: it is the "
                "separator in the hashed string, so a salt containing it would let "
                "two different (salt, case) pairs draw the same number"
            )
        if value.strip() != value:
            raise ValueError("experiment_salt may not have leading or trailing space")
        return value

    @field_validator("arm_weights_permille")
    @classmethod
    def _weights_are_complete_and_exact(cls, value: Mapping[Arm, int]) -> Mapping[Arm, int]:
        missing = set(Arm) - set(value)
        if missing:
            raise ValueError(
                f"arm_weights_permille omits {sorted(a.value for a in missing)}; "
                "every arm must appear, with 0 to switch one off explicitly"
            )
        if any(w < 0 for w in value.values()):
            raise ValueError("arm weights may not be negative")
        total = sum(value.values())
        if total != PERMILLE_TOTAL:
            raise ValueError(
                f"arm weights sum to {total} permille, not {PERMILLE_TOTAL}; "
                "normalising silently would change every share and make the run "
                "irreproducible"
            )
        return dict(value)

    @model_validator(mode="after")
    def _the_headline_comparison_is_well_formed(self) -> "ExperimentSpec":
        if self.control_arm is self.treatment_arm:
            raise ValueError(
                "control_arm and treatment_arm are the same arm; that comparison is "
                "identically zero"
            )
        for role, arm in (("control_arm", self.control_arm), ("treatment_arm", self.treatment_arm)):
            if self.arm_weights_permille[arm] == 0:
                raise ValueError(
                    f"{role} is {arm.value}, which has zero weight: the headline "
                    "comparison would have no cases on one side"
                )
        return self

    @field_validator("primary_metric")
    @classmethod
    def _the_primary_metric_is_the_headline(cls, value: MetricKey) -> MetricKey:
        if value is not MetricKey.NET_INCREMENTAL_RECOVERY:
            raise ValueError(
                "§13 names net incremental recovery as the headline; a batch that "
                "pre-registers a different primary metric is a different experiment"
            )
        return value

    # -- derived ----------------------------------------------------------

    @computed_field  # type: ignore[prop-decorator]
    @property
    def salt_digest(self) -> str:
        """SHA-256 of the salt. Goes on every assignment row instead of the salt
        itself (JC-40)."""
        return hashlib.sha256(self.experiment_salt.encode("utf-8")).hexdigest()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def preregistration_digest(self) -> str:
        """SHA-256 over the canonical form of the whole spec (JC-41).

        Excludes the derived digests themselves -- including them would recurse --
        but includes the salt, so the digest changes if the randomisation does.
        """
        payload = self.model_dump(
            mode="json", exclude={"salt_digest", "preregistration_digest", "block_size"}
        )
        return digest(payload)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def block_size(self) -> int:
        """The smallest block that reproduces the declared ratios exactly.

        80:320:100:100:320:80 has a GCD of 20, so a block of 50 cases holds
        4:16:5:5:16:4. Using 1000 would balance just as exactly but only after
        1,000 cases per stratum, which no stratum will see.
        """
        weights = [w for w in self.arm_weights_permille.values()]
        divisor = gcd(*weights)
        return PERMILLE_TOTAL // divisor

    def recovery_window_days(self, segment: Segment) -> int:
        """§12.1: 21 days for B2C, 45 for B2B. Fixed before the run."""
        return (
            self.b2c_recovery_window_days
            if segment in _B2C_SEGMENTS
            else self.b2b_recovery_window_days
        )


# ---------------------------------------------------------------------------
# the assignment itself
# ---------------------------------------------------------------------------


def _draw(*parts: str) -> int:
    """The one hashing primitive in this module. SHA-256 over the joined parts,
    top 8 bytes big-endian, reduced mod 1000.

    Eight bytes rather than four so that the modulo bias is ~2^-54 rather than
    ~2^-22: irrelevant either way at n=2,000, but free, and it removes a question
    a judge could reasonably ask.
    """
    joined = HASH_SEPARATOR.join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(joined).digest()[:8], "big") % PERMILLE_TOTAL


def assignment_permille(case_id: CaseId, spec: ExperimentSpec) -> int:
    """The case's draw in [0, 1000). Exposed so an assignment row can record the
    number it was assigned on, making re-verification a comparison rather than a
    re-run."""
    if not is_valid_id(case_id, "case"):
        raise ValueError(f"case_id {case_id!r} is not a well-formed case id")
    return _draw(spec.experiment_salt, case_id)


def assign_arm(case_id: CaseId, spec: ExperimentSpec) -> Arm:
    """§12.1's assignment: deterministic hash of ``case_id + experiment_salt``.

    Independent per case, so shares are correct in expectation and approximate in
    any finite stratum. See ``assign_arm_blocked`` for the exact-balance variant.
    """
    draw = assignment_permille(case_id, spec)
    cumulative = 0
    for arm in ARM_ORDER:
        cumulative += spec.arm_weights_permille[arm]
        if draw < cumulative:
            return arm
    raise AssertionError(  # pragma: no cover - the weight validator forbids this
        f"weights summed to {cumulative} permille, which the spec validator should "
        "have refused"
    )


def _block_pattern(spec: ExperimentSpec) -> tuple[Arm, ...]:
    """One block's worth of arms, in declaration order, before permutation."""
    divisor = gcd(*spec.arm_weights_permille.values())
    pattern: list[Arm] = []
    for arm in ARM_ORDER:
        pattern.extend([arm] * (spec.arm_weights_permille[arm] // divisor))
    return tuple(pattern)


def _permuted_block(spec: ExperimentSpec, stratum: StratumKey, block_index: int) -> tuple[Arm, ...]:
    """The block pattern shuffled by a Fisher-Yates driven from a SHA-256 stream.

    ``random.Random`` is deliberately avoided: its Mersenne Twister seeding is
    stable today but is not a documented cross-version guarantee, and this ordering
    has to reproduce on a judge's machine years from now. A hash stream is.
    """
    items = list(_block_pattern(spec))
    seed = f"{spec.experiment_salt}{HASH_SEPARATOR}{stratum.key}{HASH_SEPARATOR}{block_index}"
    stream = hashlib.sha256(seed.encode("utf-8")).digest()
    cursor = 0
    for i in range(len(items) - 1, 0, -1):
        if cursor + 4 > len(stream):  # extend deterministically, never wrap
            stream = hashlib.sha256(stream).digest()
            cursor = 0
        draw = int.from_bytes(stream[cursor : cursor + 4], "big")
        cursor += 4
        j = draw % (i + 1)
        items[i], items[j] = items[j], items[i]
    return tuple(items)


def assign_arm_blocked(
    case_id: CaseId,
    spec: ExperimentSpec,
    *,
    stratum: StratumKey,
    within_stratum_rank: int,
) -> Arm:
    """Permuted-block assignment within a stratum (JC-39).

    ``within_stratum_rank`` is the case's 0-based arrival position *within its
    stratum*, which the caller owns: a global counter would break the balance
    guarantee, and a per-stratum counter is a single ``COUNT(*)`` at case creation.

    ``case_id`` is accepted for validation and symmetry with ``assign_arm`` but
    deliberately does not enter the result -- mixing it in would restore the
    binomial wobble that blocking exists to remove.
    """
    if not is_valid_id(case_id, "case"):
        raise ValueError(f"case_id {case_id!r} is not a well-formed case id")
    if within_stratum_rank < 0:
        raise ValueError(
            f"within_stratum_rank must be >= 0, got {within_stratum_rank}"
        )
    size = spec.block_size
    block = _permuted_block(spec, stratum, within_stratum_rank // size)
    return block[within_stratum_rank % size]


# ---------------------------------------------------------------------------
# the immutable record
# ---------------------------------------------------------------------------


class ArmAssignment(BaseModel):
    """§12.1: "Logged at creation, immutable."

    Everything needed to re-derive and check the assignment later, with the salt
    itself withheld (JC-40).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: CaseId
    experiment_id: str = Field(min_length=5, max_length=64)
    arm: Arm
    stratum: StratumKey
    method: AssignmentMethod
    draw_permille: int | None = Field(
        default=None,
        ge=0,
        lt=PERMILLE_TOTAL,
        description="The hash draw, for HASH_INDEPENDENT rows.",
    )
    within_stratum_rank: int | None = Field(
        default=None, ge=0, description="The block position, for PERMUTED_BLOCK rows."
    )
    salt_digest: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="Which randomisation this row belongs to (JC-40).",
    )
    assigned_at: UtcDatetime
    stratum_definition_version: str = STRATUM_DEFINITION_VERSION
    assignment_version: str = ARM_ASSIGNMENT_VERSION

    @model_validator(mode="after")
    def _the_method_matches_the_evidence(self) -> "ArmAssignment":
        """A row must carry exactly the provenance its method produces, so that
        re-verification is never ambiguous about which recipe to re-run."""
        if self.method is AssignmentMethod.HASH_INDEPENDENT:
            if self.draw_permille is None:
                raise ValueError("a hash_independent assignment must record its draw")
            if self.within_stratum_rank is not None:
                raise ValueError(
                    "a hash_independent assignment has no block rank; a row "
                    "carrying both cannot be unambiguously re-verified"
                )
        else:
            if self.within_stratum_rank is None:
                raise ValueError("a permuted_block assignment must record its rank")
            if self.draw_permille is not None:
                raise ValueError("a permuted_block assignment has no hash draw")
        return self


class AssignmentVerification(BaseModel):
    """Why an assignment did or did not re-derive. A model, not a bool, because
    "which check failed" is the only actionable part."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    is_valid: bool
    case_id: CaseId
    expected_arm: Arm | None = None
    recorded_arm: Arm
    reason: str = Field(default="", max_length=500)


def verify_assignment(
    record: ArmAssignment, spec: ExperimentSpec
) -> AssignmentVerification:
    """Re-derive ``record`` from ``spec`` and report the first disagreement.

    The failure this is really for is not tampering, it is a forgotten re-run:
    someone changes the salt, re-randomises, and then compares arms assigned under
    two different randomisations. The salt digest catches that in one line.
    """
    if record.experiment_id != spec.experiment_id:
        return AssignmentVerification(
            is_valid=False,
            case_id=record.case_id,
            recorded_arm=record.arm,
            reason=(
                f"record belongs to experiment {record.experiment_id!r}, spec is "
                f"{spec.experiment_id!r}"
            ),
        )
    if record.salt_digest != spec.salt_digest:
        return AssignmentVerification(
            is_valid=False,
            case_id=record.case_id,
            recorded_arm=record.arm,
            reason=(
                "salt digest disagrees: this row was assigned under a different "
                "randomisation than the spec describes"
            ),
        )

    if record.method is AssignmentMethod.HASH_INDEPENDENT:
        expected = assign_arm(record.case_id, spec)
        drawn = assignment_permille(record.case_id, spec)
        if record.draw_permille != drawn:
            return AssignmentVerification(
                is_valid=False,
                case_id=record.case_id,
                expected_arm=expected,
                recorded_arm=record.arm,
                reason=(
                    f"draw disagrees: recorded {record.draw_permille}, "
                    f"re-derived {drawn}"
                ),
            )
    else:
        expected = assign_arm_blocked(
            record.case_id,
            spec,
            stratum=record.stratum,
            within_stratum_rank=record.within_stratum_rank or 0,
        )

    if record.arm is not expected:
        return AssignmentVerification(
            is_valid=False,
            case_id=record.case_id,
            expected_arm=expected,
            recorded_arm=record.arm,
            reason=(
                f"arm disagrees: recorded {record.arm.value}, re-derived "
                f"{expected.value}"
            ),
        )
    return AssignmentVerification(
        is_valid=True,
        case_id=record.case_id,
        expected_arm=expected,
        recorded_arm=record.arm,
        reason="assignment re-derived",
    )
