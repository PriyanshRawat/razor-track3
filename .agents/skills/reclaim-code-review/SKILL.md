---
name: reclaim-code-review
description: >-
  Performs deep code review of the RECLAIM AI revenue-recovery agent codebase.
  Use when reviewing any file in reclaim/contracts/ or tests/, when checking
  invariant compliance, Phase 0 freeze integrity, or preparing for Phase 1 work.
  Covers architectural layering, money safety, canonical JSON purity, audit chain
  integrity, experiment reproducibility, and the nine open questions (Q1–Q9).
---

# RECLAIM Code Review Skill

Review any file or changeset in the RECLAIM codebase against the full set of
frozen Phase 0 invariants. This skill encodes the authority hierarchy and all
structural constraints so that no review misses a load-bearing rule.

## Authority Hierarchy

When reviewing, resolve conflicts in this order:

1. **`HACKATHON_PLAN.md`** — the spec. Code cites it as `§N`.
2. **`CONTRACTS.md`** — the freeze record, especially **§3: Q1–Q9** (open questions).
3. **`CLAUDE.md`** — working-level guidance.

---

## Step 1: Understand the Dependency Layering

The module layering is acyclic and enforced by `test_contract_hygiene.py`:

```
L0  canonical  enums  ids  money  temporal  units  versions
L1  decline_taxonomy  rails  obligations
L2  strata  events  policy_format  actions
L3  metrics  case  audit
L4  experiment
```

**Violations to flag:**
- Any import from a higher layer to a lower layer (e.g., L0 importing from L1).
- Any import of `os`, `pathlib`, `subprocess`, `socket`, `sys`, `logging`,
  `asyncio`, `requests`, `httpx` in any contract module.
- Any call to `open()`, `eval()`, `exec()`, `compile()`, `__import__()` in
  contract modules.

---

## Step 2: Check the Ten Load-Bearing Invariants

For every file touched, verify these constraints:

### Invariant 1 — Money is integer paise
- `Money` stores integer paise only. No `float` touches money.
- `Decimal` is an input type (`Money.from_rupees`), never storage.
- `Money * float` must raise.

### Invariant 2 — canonical_json rejects floats at any depth
- Every number reaching audit rows or idempotency keys is `int` or fixed-scale
  `Decimal` from `units.py`.
- Probabilities: 6 dp. P-values: 9 dp. Ratios: 6 dp. Serialized as strings.
- Internal computation (bootstrap, EWMA) uses floats; conversion happens once
  at the recording boundary.

### Invariant 3 — Derived, never supplied
- `ActionEnvelope.idempotency_key` and `AuditRow.row_hash` are `computed_field`s.
- A claimed value is accepted only if it equals the derived one.

### Invariant 4 — Audit chain hashes the sequence number
- `prev_hash` chain alone cannot detect truncation (JC-27).
- The sequence number is included in the hash.

### Invariant 5 — hashlib, never builtin hash()
- Arm assignment uses `hashlib`. Never `hash()`.
- `hash()` is `PYTHONHASHSEED`-randomized and silently differs across processes.
- Weights are integer permille summing to exactly 1000.

### Invariant 6 — All timestamps are timezone-aware UTC
- Naive datetimes are validation errors, not coercions.
- Local time is derived at evaluation, never stored.

### Invariant 7 — IDs are prefixed strings
- Prefixes: `case_`, `obl_`, `mnd_`, etc.
- ID confusion is a validation error, not a silent wrong lookup.

### Invariant 8 — Action catalog is closed
- Exactly 13 write verbs. 9 `FORBIDDEN_VERBS` asserted disjoint at import time.
- Every action model is `extra="forbid"` with no free-text field.
- `send_message` has no `body`, only named slots in a registered template.

### Invariant 9 — Untrusted input is untrusted in the type system
- Inbound customer-message payload forces an untrusted envelope (JC-14).

### Invariant 10 — §14.6's ten runtime invariants
- No double debit, no contact after opt-out, no debit outside a satisfied
  notification window, recovered ≤ owed, etc.
- Violations halt the case and fail CI.

---

## Step 3: Check the Nine Open Questions (Q1–Q9)

Flag any code that touches these areas without acknowledging the open question:

| Q   | Risk      | Summary                                              |
| --- | --------- | ---------------------------------------------------- |
| Q1  | **HIGH**  | Cross-case double debit not prevented by contracts   |
| Q2  | Medium    | Stratum weighting uses at-risk money, not case count |
| Q3  | Medium    | Which assignment method for the headline run         |
| Q4  | Medium    | Arm shares are chosen, not from the plan             |
| Q5  | Low       | `event_type` is open vocabulary                      |
| Q6  | Low       | Two rail facts unverified (e-NACH, UPI Autopay)      |
| Q7  | Medium    | `contacted_case_count` is approximate                |
| Q8  | Low       | 16 rows → 17 metrics (opt-out/complaint split)       |
| Q9  | Operational | `₹` breaks Windows CLI output                     |

---

## Step 4: Review Checklist for Any Change

### Contract Modules (`reclaim/contracts/*.py`)
- [ ] No new I/O imports or calls
- [ ] No float in any serializable path
- [ ] Layer dependency respected
- [ ] Module imports standalone in a fresh interpreter
- [ ] JC decisions preserved; any new JC gets a unique number (retired: JC-04,
      JC-05, JC-07, JC-09–JC-12)
- [ ] Docstrings state the *cost* of a choice, not just the choice
- [ ] `extra="forbid"` on all Pydantic models touching the action catalog

### Tests (`tests/*.py`)
- [ ] Tests re-derive recipes independently (never call the implementation to
      compute the expectation)
- [ ] Experiment tests run under multiple `PYTHONHASHSEED` values
- [ ] `PYTHONIOENCODING=utf-8` used for all test commands
- [ ] No float assertions where Decimal is expected

### Version Bumps
- PATCH = comments only
- MINOR = additive (new enum member, new optional field)
- MAJOR = breaking (rename/remove field or enum member, change semantics)
- MAJOR bump on `ACTION_CATALOG_VERSION` or `POLICY_FORMAT_VERSION` after
  `SEED_EVAL` invalidates the scoreboard

### Phase 0 Freeze Violations (CRITICAL)
These break the freeze's guarantees:
1. Recomputing a metric inline instead of calling `metrics.py`
2. A detector writing a `float` into a payload reaching the chain
3. The experiment salt changing between pre-registration and eval run
4. Flipping `PHASE_0_FROZEN` without the §3 review

---

## Step 5: Run Verification

```bash
PYTHONIOENCODING=utf-8 python -m pytest           # full suite (242 tests)
PYTHONIOENCODING=utf-8 python -m pytest tests/test_contract_hygiene.py -q  # structural invariants
```

---

## Output Format

Structure your review as:

1. **Summary** — one paragraph assessment
2. **Critical Issues** — freeze violations or invariant breaches
3. **Warnings** — Q1–Q9 area touches, potential regressions
4. **Observations** — code quality, naming, documentation
5. **Verification** — test results and coverage gaps
