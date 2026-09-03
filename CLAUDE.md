# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository currently is

RECLAIM — an AI revenue-recovery agent (hackathon Track 03). The repo is at **Phase 0: a
contract freeze**. Everything under `reclaim/contracts/` is a Pydantic schema, an enum, or a
pure function. There are no detectors, no policy rules, no planner, no prompts, no LLM calls,
no persistence, no I/O, and no UI — that absence is deliberate and is enforced by tests.

Phase 0 is code-complete and has been through an adversarial review that found and fixed
**five defects in already-green code** (`CONTRACTS.md` §6). The freeze is **not signed off
yet**: `versions.PHASE_0_FROZEN` is still `False` and awaits the user's decision on the §3
questions (Q1/Q3/Q4 in particular).

Three documents govern the work, in this order of authority:

- **`HACKATHON_PLAN.md`** — the spec. Code and docstrings cite it as `§N` (e.g. `§12.1`,
  `§14.6`). When a comment says "§13, verbatim", the plan's table is the source of truth and
  an import-time guard usually enforces the match.
- **`CONTRACTS.md`** — what was frozen and, more importantly, **§3: the nine places the plan
  left a question open (Q1–Q9)**. Read §3 before changing anything in `reclaim/contracts/`.
  Q1 (cross-case double debit) is an unfixed hole that Phase 1 must close with a DB
  uniqueness constraint on `(obligation_id, attempt_sequence)`. **§6** records the review pass
  — read it to see which five things looked frozen but weren't.
- **`hackathon_prompt_claude_code.md`** — the original prompt that produced the plan. Historical.

## Commands

```bash
pip install -e ".[dev]"

PYTHONIOENCODING=utf-8 python -m pytest                              # full suite (251 tests)
PYTHONIOENCODING=utf-8 python -m pytest tests/test_experiment.py -q  # one file
PYTHONIOENCODING=utf-8 python -m pytest tests/test_experiment.py::test_name -q
```

**`PYTHONIOENCODING=utf-8` is not optional on this machine** (CONTRACTS.md Q9). Windows
`cp1252` raises `UnicodeEncodeError` on `₹`, which `Money.__str__` emits; any command whose
output can reach a `Money` repr will crash without it. Applies to `python -c` one-liners too.

`pyproject.toml` sets `pythonpath = ["."]` so bare `pytest` and `python -m pytest` agree —
do not remove it; a judge running the bare command must not get collection errors.

## Architecture: the contract layering

`reclaim/contracts/__init__.py` is **deliberately empty of re-exports**. Import from the
owning module (`from reclaim.contracts.actions import ActionEnvelope`), never from the
package. The layering below is published in that docstring; **`tests/test_contract_hygiene.py`
enforces acyclicity**, so inverting an edge fails the build as soon as the reverse import
exists (which, between adjacent layers, is immediately):

```
L0  canonical  enums  ids  money  temporal  units  versions
L1  decline_taxonomy  rails  obligations
L2  strata  events  policy_format  actions
L3  metrics  case  audit
L4  experiment
```

The same test file also enforces: no contract may import `os`, `pathlib`, `subprocess`,
`socket`, `sys`, `logging`, `asyncio`, `requests`, `httpx`, … or call `open()`/`eval()`/
`exec()`/`compile()`/`__import__()`; and every module must import standalone in a fresh
interpreter. This is §12.5.4's simulator-integrity separation starting at the bottom of the
stack — a contract that can reach the filesystem is somewhere for a Phase 1 detector to hide
behaviour.

## Invariants that constrain every line you write

These are structural, not stylistic. Each is pinned by a test, and breaking one is usually a
crash at serialisation time rather than a wrong number.

1. **Money is integer paise** (`money.py`). No float touches money, ever. `Decimal` is an
   *input* type (`Money.from_rupees`), never storage. `Money * float` raises.
2. **`canonical.canonical_json` rejects floats at any depth**, naming the path. Every number
   that reaches an audit row or an idempotency key is an int, or a fixed-scale `Decimal` from
   `units.py` (probabilities 6 dp, p-values 9 dp, ratios 6 dp), serialised as a string.
   Internal computation (bootstrap, model inference, EWMA) uses floats; conversion happens
   once, at the recording boundary.
3. **Derived, never supplied.** `ActionEnvelope.idempotency_key` and `AuditRow.row_hash` are
   `computed_field`s over canonical JSON. A claimed value is accepted on read only if it
   equals the derived one — an edited stored row does not parse, let alone verify.
4. **The audit chain hashes the sequence number too** (`audit.py`, JC-27). A pure `prev_hash`
   chain cannot detect truncation.
5. **Arm assignment uses `hashlib`, never the builtin `hash()`** (`experiment.py`, JC-37).
   Builtin `hash()` is `PYTHONHASHSEED`-randomised: it passes in-process tests and silently
   differs between the pre-registration run and the judge's reproduction. Weights are integer
   permille summing to exactly 1000 — never float shares.
6. **All timestamps are timezone-aware UTC** (`temporal.py`). A naive datetime is a
   validation error, not a coercion; quiet-hours correctness in the payer's zone depends on
   it. Local time is derived at evaluation, never stored.
7. **IDs are prefixed strings** (`ids.py`, e.g. `case_`, `obl_`, `mnd_`), so ID confusion is
   a validation error rather than a silent wrong lookup.
8. **The action catalog is closed** (`actions.py`). Exactly thirteen write verbs; the nine
   `FORBIDDEN_VERBS` are asserted disjoint at import time. Every action model is
   `extra="forbid"` with no free-text field — `send_message` has no `body`, only named slots
   in a registered template. This is why §14.4's answer to "what if the model is fully
   compromised?" is structural. An import-time guard also checks that every `ActionSpec`'s
   `channel_field` names a real field on its model — §14.1's consent, quiet-hours and
   frequency gates read the channel through that name, so a spec pointing at a missing field
   is an `AttributeError` on the compliance path, not a spare-attribute typo (review §6).
9. **Untrusted input is untrusted in the type system** (`events.py`, JC-14): an inbound
   customer-message payload forces an untrusted envelope.
10. **§14.6's ten runtime invariants** (no double debit, no contact after opt-out, no debit
    outside a satisfied notification window, recovered ≤ owed, …) are what Phase 1's checker
    must assert on every transition. A violation halts the case and fails CI.

## Conventions

- **`CONTRACT DECISION (JC-nn)`** in a module docstring is the full argument for a judgment
  call; `CONTRACTS.md` §2 is the index. **JC numbers are retired, never reused** — JC-04,
  JC-05, JC-07 and JC-09–JC-12 are permanently unallocated, so a `JC-nn` in a commit message
  resolves to exactly one decision. Add new decisions in the module docstring *and* the index.
- Docstrings state the *cost* of a choice, not just the choice. Match that register: a
  comment that says what the code does is noise; one that says what breaks if you change it
  is the point.
- Tests **re-derive recipes independently** rather than asking the module (see
  `tests/test_experiment.py`, which recomputes the assignment hash from the documented recipe
  and runs the assigner in subprocesses under three `PYTHONHASHSEED` values). Preserve that
  when editing tests — a test that calls the implementation to compute its expectation cannot
  catch a silent re-randomisation of 2,000 cases.
- Test files group several contracts (`test_units_and_rails.py` covers units, rails, money,
  ids, temporal, versions); they are not 1:1 with modules.
- Rail *mechanics* (regulatory floors, cited, in `rails.py`) and policy *thresholds* (numbers
  we choose, in `policy_format.py`) have different owners. Config may only add safety margin
  on top of a floor, never subtract. Two rail facts are carried as `verify_before_demo`
  notes (`rails.py:139`, `rails.py:156`) — do not assert either to a judge unchecked.
- **A table is not frozen because it reads correctly; it is frozen when a test walks every
  row.** Four of the five review defects (CONTRACTS.md §6) lived in mappings that were
  asserted only by eye — `ALLOWED_CASE_TRANSITIONS` had a live state with no `STOPPED` edge,
  `ACTION_SPECS` named a field its model lacked. Green tests measured the code that had tests.
  When you add or edit an enum-keyed mapping (`ACTION_SPECS`, `PAYLOAD_MODELS`,
  `DECLINE_CLASS_META`, `ALLOWED_CASE_TRANSITIONS`, the `METRIC_SPECS`/§13 guard), add or
  extend the test that iterates its keys — not one that spot-checks a member.

## Working on Phase 1

`versions.PHASE_0_FROZEN` is still `False`, and `test_contract_hygiene.py` asserts it. Flip it
only after the CONTRACTS.md §3 review, and delete that test in the same change.

Version bumps (`versions.py`): PATCH = comments only; MINOR = additive; MAJOR = breaking. A
MAJOR bump of `ACTION_CATALOG_VERSION` or `POLICY_FORMAT_VERSION` after the `SEED_EVAL` run
invalidates the reported scoreboard. `CANONICAL_JSON_VERSION` re-hashes the world.

Four things would break the freeze's guarantees, per CONTRACTS.md §5:

- recomputing a metric inline instead of calling `metrics.py`;
- a detector writing a `float` into a payload that reaches the chain;
- the experiment salt changing between the pre-registration commit and the eval run;
- flipping `PHASE_0_FROZEN` without the review.

Phase 1 must also supply the real `contacted_case_count` (currently approximated as
`min(at_risk_case_count, outbound_contacts)`, `metrics.py:443`, with two guardrail tests
depending on the approximation), and `IncrementalRecoveryEstimate` declares CI fields that no
code yet computes — bootstrap resampling is Phase 1's.
