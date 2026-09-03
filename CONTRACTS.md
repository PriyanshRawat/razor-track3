# CONTRACTS.md — the Phase 0 freeze

**Status:** Phase 0 complete, awaiting review. `versions.PHASE_0_FROZEN` is still
`False` and a test asserts it, so nothing can claim the freeze before you sign it off.

**What this document is for.** Two things, and the second matters more than the first:
a map of what was frozen, and an explicit list of **the places `HACKATHON_PLAN.md`
did not settle a question and I had to choose**. Every item in §3 is a decision you
can reverse cheaply now and expensively later.

Verify the tree yourself:

```bash
python -m pytest
```

407 tests, all passing. **299** of them are the contracts in §1 — that is the
number this document is about. The other 108 belong to the Phase 1 work since built
on top of the freeze: the persistence spine, the decline-code normaliser and the
deterministic diagnostician. Still absent, and still deliberate: no policy rules, no
planner, no prompts, no LLM calls, no UI.

*(Lineage of the contract tests: 242 were the freeze itself; nine more came from the
adversarial review in §6; the rest from the Q10 amendment and the two §7 passes.
Every one of those later tests was watched to fail before its fix landed.)*

---

## 1. What was frozen

| # | Deliverable | Module | Tests |
|---|---|---|---|
| 1 | Canonical event / obligation schema | `events.py`, `obligations.py` | `test_obligations.py` (15), plus `test_units_and_rails.py`, `test_case_and_audit.py` |
| 2 | Decline-code taxonomy | `decline_taxonomy.py` | `test_actions.py` |
| 3 | **Typed action catalog** | `actions.py` (971 lines) | `test_actions.py` (58) |
| 4 | Policy rule *format* only | `policy_format.py` | `test_policy_format.py` (52) |
| 5 | RiskCase / Diagnosis / Plan / AuditRow | `case.py`, `audit.py` | `test_case_and_audit.py` (54) |
| 6 | Metric definitions (§13) | `metrics.py` | `test_metrics.py` (40) |
| 7 | Deterministic arm assignment | `experiment.py` | `test_experiment.py` (44) |
| — | Rails, money, units, ids, time, versions | `rails.py`, `money.py`, `units.py`, `ids.py`, `temporal.py`, `versions.py` | `test_units_and_rails.py` (13) |
| — | Structural invariants of the package itself | — | `test_contract_hygiene.py` (23) |

Dependency layering is acyclic and enforced by a test, as is the rule that no
contract imports `os`, `pathlib`, `socket`, `subprocess`, `requests`, or calls
`open()`/`eval()`. That is §12.5.4's sim-integrity separation starting at the
bottom of the stack.

### The five load-bearing mechanisms

1. **Money is integer paise.** No float touches money, ever. `canonical_json`
   *raises* on a float anywhere in a payload, at any depth, naming the path.
2. **Statistics are quantised `Decimal`s serialised as fixed-point strings** —
   probabilities to 6 dp, p-values to 9, ratios to 6. Never scientific notation.
3. **Idempotency keys are derived, never supplied.** `ActionEnvelope.idempotency_key`
   is a `computed_field` over the canonical JSON of the action's scope. A claimed key
   is accepted on read only if it equals the derived one.
4. **The audit log is a hash chain with numbered rows.** `AuditRow.row_hash` is
   derived the same way; an edited stored row does not verify and does not even parse.
5. **Arm assignment is `hashlib`, reproducible across processes.** Pinned by a
   subprocess test under three `PYTHONHASHSEED` values, and by a test that
   recomputes the recipe independently rather than asking the module.

---

## 2. The judgment calls, by module

Each is written out in full in the module's docstring under
`CONTRACT DECISION (JC-nn)`. Summarised here so a reviewer needs one file, not
eighteen.

**Foundations**
- **JC-01** `money.py` — money is integer paise; `Decimal` is an input type, never storage.
- **JC-02** `enums.py`, `strata.py` — amount-band boundaries mirror policy thresholds but are frozen independently; a stratum is stored at case creation and never recomputed.
- **JC-03** `ids.py` — IDs are prefixed strings, not UUIDs, so an ID-confusion bug is a validation error.
- **JC-15** `units.py` — every reported statistic is a fixed-scale `Decimal`; floats are rejected at the canonical-serialisation boundary.
- **JC-16** `rails.py` — rail *mechanics* (regulatory floors, cited) and policy *thresholds* (numbers we choose) have different owners, and config may only ever add safety margin on top of a floor.

**Domain**
- **JC-06** `enums.py` — the plan's "≤5-step conditional plan" is modelled as a bounded step ladder with typed triggers.
- **JC-08** `enums.py` — `A5` disables the policy engine, so it is the one arm whose violations are expected and must be reported, not suppressed.
- **JC-13** `events.py` — event payloads are a discriminated union on a literal tag; the envelope's type and payload tag must agree.
- **JC-14** `events.py` — an inbound customer message is **untrusted by construction**, and an untrusted payload forces an untrusted envelope (prompt-injection defence starts in the type system).
- **JC-17/18/19** `actions.py` — every outbound message goes through a registered template; catalog tiers are *floors*, not verdicts (low confidence tiers **up**); and the idempotency key deliberately does **not** protect against a cross-case double debit (see Q1).
- **JC-20/21/22** `policy_format.py` — effects compose as a lattice `DENY > DEFER > ALLOW_WITH_APPROVAL > ALLOW`, not by rule priority; an advisory verdict never blocks; thresholds we choose live here, rail floors do not.
- **JC-23…26** `case.py` — a case stores its own stratum and arm; a claim without evidence cannot be constructed; abstention is structural (`UNKNOWN` may not carry high confidence); a plan is a bounded, contiguous, single-case ladder.
- **JC-27…31** `audit.py` — rows are numbered *and the number is hashed* (a pure `prev_hash` chain cannot detect truncation); the row hash is derived and a claimed one is checked; digests, not payloads; every verdict recorded including allows; `event_type` is a validated snake_case string rather than an enum (see Q5).
- **JC-42** `case.py`, `strata.py` — a D1 (failed-debit) case stratifies on its normalised `DeclineClass` and records it in `canonical_decline_class`; every other risk class stays restricted to its own `RiskClass` value (amendment; see Q10).
- **JC-43** `policy_format.py` — quiet hours have exactly one owner: a payer's stated window wins, in their own zone; absent one, the configured window read as **Asia/Kolkata**. Decided in `resolve_quiet_hours` and nowhere else, which is why `ConsentProfile.quiet_hours` had to become optional (amendment; see §7 N7).

**Measurement**
- **JC-32…36** `metrics.py` — specs and formulas ship together with an import-time guard against §13; a rate with no denominator is `None`, not zero; rates are fixed-scale `Decimal`s; the estimator weights by at-risk money (see Q2); CIs are declared here and computed in Phase 1.
- **JC-37…41** `experiment.py` — `hashlib` never builtin `hash()`; weights are integer permille summing to exactly 1000; two assignment methods with the record naming which (see Q3); the record carries the salt's *digest*, not the salt; the spec hashes itself into a pre-registration digest.

*Numbering note:* JC-04, JC-05, JC-07 and JC-09–JC-12 were allocated during the
design pass and never ended up anchored in code. They are **retired, not reused**,
so a `JC-nn` reference in a commit message always resolves to exactly one decision.
The decisions those modules do carry are in their module docstrings under prose
headings (`decline_taxonomy.py` "The load-bearing distinction", `temporal.py` "Two
rules, frozen", `obligations.py` "Key contracts encoded here").

---

## 3. Where `HACKATHON_PLAN.md` did not fully specify — **please review these**

Ordered by what it costs to change them later.

### Q1. A cross-case double debit is not prevented by the contracts *(highest risk)*
`case_id` is part of every action's idempotency scope, so two different cases on the
same obligation derive **different** keys and both could schedule a debit.
- **What I did:** left it, and documented that the real protection is a uniqueness
  constraint on `(obligation_id, attempt_sequence)` in the Phase 1 execution store.
- **Why not fix it here:** removing `case_id` from `ScheduleDebit`'s scope makes two
  legitimately distinct cases on one obligation collide instead — a worse failure.
- **What it costs if we forget:** a real double debit in the demo, which is the single
  most damaging thing that could happen on stage. **This must become a Phase 1
  acceptance test, not a note.** `actions.py:862`.

### Q2. The stratum-weighted estimator weights by at-risk money, not case count
§12.1 says "stratum-weighted" and does not say by what.
- **What I did:** weighted by at-risk **money**. A stratum holding 90% of the rupees
  dominates the estimate even if it holds few cases.
- **Why:** the headline is a rupee number, so a case-count weighting would let a
  regression in the high-value band hide behind a large population of small cases.
- **The cost:** a single ₹10L case in a thin stratum can move the headline more than
  a hundred ₹1,499 cases, which widens the CI. `metrics.py`, JC-35.

### Q3. Which assignment method the headline run uses
§12.1 specifies independent hashing of `case_id + salt`, then calls the design
"stratified" — which as written is *post*-stratification, with no within-stratum
balance guarantee.
- **What I did:** shipped **both**. `assign_arm` is the plan-literal version;
  `assign_arm_blocked` gives exact balance every 50 cases within a stratum
  (permuted blocks, block size derived from the weights' GCD).
- **The trade:** blocking fixes the real problem — with 6 arms, a stratum of 40 cases
  routinely leaves one arm with 2 cases — but it needs a per-stratum arrival counter
  and makes the next arm predictable from (salt, stratum, rank).
- **Default today:** `assign_arm` (independent). **Your call before the eval run.**

### Q4. Arm shares are mine, not the plan's
§12.1 says control + treatment "plus the ablation arms below on a smaller share" and
gives no numbers. I chose **A0 8% / A1 32% / A2 10% / A3 10% / A4 32% / A5 8%**.
- **The consequence, stated plainly:** at n=2,000 the A1-vs-A4 headline gets ~640
  cases per arm, which is the widest-usable split. The ablation arms get 200 cases
  each (A2/A3) and 160 (A0/A5). **A4−A3 — "the value of the LLM" — is underpowered
  at that size and will not reach significance unless the effect is large.** §12.2
  already anticipates this ("we will report the subgroups where it concentrates"),
  but the contract now makes the arithmetic explicit rather than discovering it on
  scoreboard day. If A4−A3 must carry a CI, either n rises or the ablation arms take
  share from the headline pair.

### Q5. `AuditRow.event_type` is the one open vocabulary in Phase 0
Everything else is a closed enum. The audit log's own lifecycle vocabulary is a
validated snake_case string, with the 22 currently-known log points recorded in
`AUDIT_EVENT_TYPES` but **not enforced**.
- **Why:** Phase 1 will add log points, and amending a frozen contract for each one
  invites the worse outcome — reusing a wrong-but-frozen member.
- **The cost:** two spellings of the same event can coexist and split a group-by.
  Cheap to close later (freeze the set once Phase 1's log points are known).

### Q6. Two rail facts the plan's sources do not settle
Both are carried in code as `verify_before_demo` notes rather than confident numbers,
so they are impossible to ship as silent assumptions:
- **e-NACH AFA step-up** (`rails.py:156`). Whether the per-debit AFA step-up above the
  threshold applies to NACH mandates as it does to card and UPI e-mandates is not
  settled. Modelled as **not supported**, which makes a dead e-NACH mandate a
  re-registration case. If that is wrong, we are over-cautious — the safe direction,
  but it changes which intervention the router picks.
- **UPI Autopay submission horizon** (`rails.py:139`). The 26-hour pre-charge delay is
  documented for India *cards*. I assumed UPI Autopay's horizon equals the 24h
  notification floor. **Do not assert this to a judge before checking it.**

### Q7. `ArmOutcome.contacted_case_count` is an approximation
Opt-out and complaint rates are per *contacted case*, and Phase 0 has no
case-level contact table, so it is approximated as
`min(at_risk_case_count, outbound_contacts)`. Two guardrail tests depend on it.
Phase 1 must supply the true figure; until then the two rates are directionally
right and numerically approximate. `metrics.py:443`.

### Q8. §13 has 16 rows; the code defines 17 metrics
"Opt-out & complaint rate" is one row naming two separately measured quantities, so
it maps to `OPT_OUT_RATE` and `COMPLAINT_RATE`. This is the **only** place the table
is not 1:1 with code, an import-time guard enforces it, and a test asserts the split
is exactly those two keys.

### Q9. Operational: `₹` breaks CLI output on this machine
Windows `cp1252` raises `UnicodeEncodeError` on `₹`, which `Money.__str__` emits.
Every command in this repo needs `PYTHONIOENCODING=utf-8`, or Phase 1's
`verify_chain` CLI needs an ASCII fallback. Not a contract defect; it will bite
during the demo if ignored.

### Q10. A D1 case could not carry its own decline class — **amended, 2026-09-01**
Unlike Q1–Q9, this one is **closed, not open**: it was a defect rather than an
under-specification, and the amendment is in the code. Logged here in Q1's format so
the change is reviewable beside the questions it sits among.

**What was wrong.** `RiskCase._stratum_agrees_with_the_case` required
`stratum.failure_class == risk_class.value` on *every* case. `StratumKey` documents
two legal vocabularies for that axis — a `DeclineClass` for cases that observe a PSP
decline code, a `RiskClass` for those that do not — so the validator made the first
half unreachable. Nothing in the system could construct a case stratified on
`insufficient_funds`. Two consequences, one worse than it looks:
- The normalised decline class had **no field on the case at all**, while
  `events.py:249` says a raw code is "normalised downstream against the taxonomy
  version recorded on the case".
- §12.1 stratifies by "amount band × failure class × segment". With the failure-class
  axis pinned to the detector's own name, every D1 case in `seed.py` landed in **one**
  stratum where the design calls for four — a stratified estimator with nothing to
  stratify on. `seed.py` even drew a `DeclineClass` per case and discarded it.

**Why 251 tests did not catch it.** Every fixture in the suite passed a `RiskClass` on
every axis — `conftest.py`'s `make_case`, `test_case_and_audit.py`'s `_stratum`,
`test_experiment.py`, `test_metrics.py`. The two tests that *looked* like coverage
were the near-miss: `test_a_stratum_whose_failure_class_disagrees_with_the_case_is_rejected`
asserts a wrong `RiskClass` is rejected, and `test_spine_seed`'s
`test_stratum_agrees_with_case` accepted a D1 decline class by asserting only that
the value *differed* from the risk class — which passes for any string in either
vocabulary. This is CLAUDE.md's rule again in a new place: **a table is not frozen
because it reads correctly**. Here it was a *vocabulary* asserted only by eye.

**What changed.**
- `strata.py` — new `legal_failure_classes_for(risk_class)`. The existing flat
  `legal_failure_classes()` stays and is now documented as deliberately unscoped: a
  `StratumKey` is validated without knowing which risk class produced it, so it can
  only ask "is this a word in either vocabulary".
- `case.py` (JC-42) — the validator now checks the stratum against the *scoped*
  vocabulary, and a new `canonical_decline_class: DeclineClass | None = None` field
  records the class. A second validator ties the two together: a stratum carrying a
  decline class the case does not name is rejected (an untraceable bucket), while a
  case naming a class its frozen stratum predates is allowed (normalisation can land
  after detection, and the stratum freezes at detection per JC-23).
- `conftest.py` — `make_case` now defaults a D1 case to
  `PAYER_AUTHORIZATION_MISSING_AMBIGUOUS`, so the fixture that flows through the
  spine tests exercises the vocabulary that was unreachable.
- `seed.py` — passes the decline class it already drew instead of discarding it.
- Tests — the D1 vocabulary is now walked member by member (`for decline_class in
  DeclineClass`), not spot-checked, and `test_spine_seed` asserts the seeded D1 cases
  span ≥3 distinct classes.
- `CONTRACTS_SCHEMA_VERSION` 1.0.0 → **1.1.0**: additive optional field, so a 1.0.0
  payload still parses.

**What it does NOT cover — D2.** `PREDICTED_TO_FAIL_DEBIT` is deliberately left
exactly where it was: restricted to its own `RiskClass` value. A prediction has no
observed decline code, so stratifying it on one would mean stratifying on a
*predicted* class — a different object, with its own error rate, feeding the
weighting key of the headline estimate. That is the D2 detector's question to answer
when D2 is built, and guessing at it now would freeze an answer nobody has reasoned
about. **If D2 is built, this question must be reopened before its first case is
opened**, because the stratum is immutable from creation (JC-23).

**Not covered either:** `events.py` promises a *taxonomy version* "recorded on the
case", and there is still no field for it. JC-02 anticipates a
`stratum_definition_version` in Phase 1; the taxonomy version belongs beside it. Until
both exist, a decline class re-mapped between runs is not detectable from the case.

**What it costs.** Two D1 cases identical in amount, segment and root cause can now
land in *different* strata depending on whether normalisation beat detection — the
risk-class fallback is a second regime, not just a null. Phase 1's detector must
normalise before opening the case so the fallback stays an escape hatch. **This is a
weakening of the "stratum was derived from this case" guarantee, traded for making
the documented design reachable at all.**

---

## 4. What Phase 0 deliberately does **not** contain

No detectors. No policy rules (only their format). No planner, no prompts, no LLM
calls. No per-PSP decline-code mapping table beyond the seed rows the plan quotes
verbatim. No bootstrap resampling — `IncrementalRecoveryEstimate` declares the CI
fields and leaves them unset. No persistence, no I/O of any kind. No UI.

## 5. Honest assessment of the freeze

**Strengths, with evidence.** The two mechanisms a judge can attack directly — "how
do you know it was you?" and "how do we know the log is real?" — are enforced in the
type system rather than in review comments: 251 tests, and the four mutations I
introduced deliberately (builtin `hash()`, an off-by-one in the cumulative arm walk,
a block size that destroys exact balance, a permutation that ignores the stratum)
were each caught by the tests that should catch them. The freeze also survived being
attacked on purpose: **eight defects were found and fixed before this document was
signed off** — three during construction (including a `float` p-value on
`SystemicIncident` that would have raised inside `canonical_json` at the moment of
writing a suppressed cohort's audit row) and five in the adversarial review recorded
in §6. Five of the eight were latent crashes or silent wrong answers on the
compliance and measurement paths, not style.

**Weaknesses, unprompted.** (a) Q1 is a genuine hole that contracts cannot close;
it is a Phase 1 database constraint or it is a bug. (b) Q4 means the ablation ladder
is decoration for individual claims at n=2,000 — the honest report is A4−A3 by
subgroup, not a headline CI. (c) `contacted_case_count` (Q7) is an approximation
that two guardrail tests currently depend on. (d) Nothing here has met real data;
every rejection path is proven against inputs I chose, and the first ingest of a
real Stripe test-mode webhook will find fields I did not model. (e) §6 is the
uncomfortable one: **a review found five real defects in code that was already fully
green**, four of them in exactly the places that had no test — so the residual risk
is concentrated wherever a table or mapping is still asserted only by inspection.

**Ceiling.** This freeze can deliver a reproducible, tamper-evident, randomised
evaluation whose headline number survives an adversarial finance judge. It cannot
make the LLM's marginal value significant at n=2,000, and it cannot prove any
absolute recovery rate — only a difference between arms in a calibrated environment.

**What would make it fail.** Phase 1 recomputing a metric inline instead of calling
`metrics.py`; a detector writing a `float` into a payload that reaches the chain; the
arm salt changing between the pre-registration commit and the eval run;
`PHASE_0_FROZEN` being flipped without the review in §3.

---

## 6. The review pass — five defects found and fixed

Phase 0 was defined as ending when the contracts were done **and reviewed**, so the
frozen tree was handed to an independent reviewer agent with no knowledge of the
design intent. All five findings below were **verified against the code before being
acted on** (one was reported inaccurately and was in fact worse — see finding 2), each
fix was driven by a test watched to fail first, and the suite went 242 → 251.

| # | Where | Defect | Class |
|---|---|---|---|
| 1 | `actions.py` | `InitiateVoiceCall` had no `channel` field, but its `ActionSpec` declared `channel_field="channel"` | crash on the compliance path |
| 2 | `policy_format.py` | a probability predicate had no `Decimal` branch — the value was **silently coerced to a 1970 datetime** | silent wrong answer |
| 3 | `enums.py` | `CaseState.RETRY_BACKOFF` had no transition to `STOPPED` | a case that cannot be stopped |
| 4 | `metrics.py` | `opt_outs`/`complaints` were unbounded above `contacted_case_count` | guardrail crashes instead of failing closed |
| 5 | `case.py` | the stratum agreement check verified the segment only, not `amount_band` or `failure_class` | wrong rupees in the headline bucket |

A **sixth** defect of the same shape was found later and is written up as Q10: the
stratum agreement check that finding 5 tightened was tightened onto the *wrong*
vocabulary, so a D1 case could never carry a `DeclineClass` at all. Finding 5 and Q10
are the same lesson twice — the check read correctly, and no fixture ever exercised
the branch it forbade.

Two of these deserve to be read in full rather than as a row:

**Finding 2 was more severe than reported.** The reviewer said a `Decimal` compared
against a probability fact "fails validation outright". It did not — pydantic's union
resolution handed `Decimal("0.55")` to `UtcDatetime` and produced
`1970-01-01T00:00:00.55Z`. Every confidence threshold in §14.2's "low confidence tiers
up" would have been a predicate that **reads correctly and matches everything**. The
fix is a `Decimal` branch with `Field(strict=True)` — strictness is load-bearing twice,
once for JC-15 (no float becomes a Decimal by coercion) and once to stop the union
from reaching `UtcDatetime` at all — plus an explicit error for the string case, since
`'0.9' < '0.55'` is `False` and a lexicographic comparison would also have read as
intended and evaluated backwards.

**Finding 1 generalised into an import-time guard.** The missing field was one
instance; the class is "a spec names a field by string and nothing checks the field
exists". `ACTION_SPECS` is now walked at import and any `channel_field` that is not a
real field on its model raises. Without it, the next contact verb added in Phase 1
fails as an `AttributeError` inside a consent or quiet-hours gate.

**What the review did not cover, and what I did about it.** The reviewer disclosed
that `events.py` and `decline_taxonomy.py` "were read early and only skimmed on the
second pass", so I re-verified both directly: an inbound customer message cannot claim
`system_of_record`, `psp_verified`, `human_asserted` or `bank_feed` trust and cannot
set `untrusted=False` (JC-14 holds); `PAYLOAD_MODELS` is total over `EventType`; and
`DECLINE_CLASS_META` is total over `DeclineClass`, with every unrecognised raw code
resolving to `unknown_unmapped` / `unknown_fail_closed` rather than a guess. I also
swept all eighteen modules for the two defect *classes* rather than the instances:
zero `float` annotations anywhere, zero non-total enum-keyed mappings, and
`channel_field` is the only attribute that refers to another field by name.

**The pattern worth keeping.** Findings 1, 3, 4 and 5 were all in code that had no
test — `ALLOWED_CASE_TRANSITIONS` was asserted by reading it, `ACTION_SPECS`
consistency by reading it. Green tests measured the code that had tests. For Phase 1
that means: a table is not frozen because it looks right, it is frozen when a test
walks every row of it.

---

## 7. The second review pass — 23 cross-module mismatches

§6 found five defects of one shape; Q10 was the sixth. This pass read all nineteen
contract modules end to end — docstrings, field descriptions and validators
together — looking for **that shape only**: *module A's prose asserts X, module B's
code permits ¬X, and no fixture ever exercises the disagreeing path.* Every row
below whose entry describes a behaviour was executed, not inferred.

Two corrections to §6's own closing sweep, which claimed "zero non-total enum-keyed
mappings" and treated `channel_field` as the only cross-module name reference:
**N1 is a non-total enum-keyed mapping** — missed because it is a
`Field(default_factory=...)` rather than a module-level table — and N3/N5/N9 are
three more references that cross a module boundary by convention rather than by
type. That sweep was right about the instances it checked and wrong that it had
enumerated the class.

| # | Modules that disagree | The mismatch | Why it matters | Status |
|---|---|---|---|---|
| N1 | `policy_format` vs every other enum-keyed table | `max_grace_period_days_by_segment: Mapping[Segment,int]` has no totality guard; `DECLINE_CLASS_META`, `RAIL_SPECS`, `ACTION_SPECS`, `PAYLOAD_MODELS`, `FACT_TYPES` all do | YAML-loaded, so a real config omission is a `KeyError` inside the policy engine | **FIXED** 2026-09-01 |
| N2 | `policy_format` internal | `FactPredicate` validates MONEY/BOOLEAN/PROBABILITY/COUNT/DURATION and has **no branch for TIMESTAMP or ENUM**: `QUIET_HOURS_END_AT gte 5` and `RAIL eq "card_emandat"` are both accepted | §6-2's defect class with two fact types still open. A typo'd enum literal is a rule that loads, tests clean, and never fires | **FIXED** 2026-09-01 |
| N3 | `obligations` vs `events` | `PaymentReceivedPayload.match_method` is a `Literal`; `PartialPayment.match_method` was `str` and accepted `"inferred_from_text"` — the one value its own description forbids | §14.4's defence held on the event and evaporated on the ledger row that event writes | **FIXED** 2026-09-01 |
| N4 | `obligations` internal | `record_for` returns the first channel match and nothing forbade two. The same two records reversed gave `has_consent` = False vs True | The gate for "no contact after opt-out" depended on tuple order | **FIXED** 2026-09-01 |
| N5 | `obligations` vs `enums` | `Hold.kind` was a `str` listing seven values in prose while `StopReason` already held that closed vocabulary; `kind="optout"` was accepted | A typo'd hold kind silently disables an immediate hard stop | **FIXED** 2026-09-01 |
| N6 | `actions` internal | `schedule_debit`'s `policy_categories` omit `financial_authority` and `timing` while its own `guard_summary` names the AFA threshold and the 24h notification | `policy_categories` is what drives a generic engine, so the only money-moving verb would skip financial-authority rules | **FIXED** 2026-09-01 (`financial_authority` only — see below) |
| N7 | `obligations` vs `policy_format` vs `temporal` | Quiet hours live in two places — `ConsentProfile.quiet_hours` (per payer, carries a timezone) and `PolicyThresholds.quiet_hours_*` (global, **no timezone**) — with no precedence rule | Both are legitimate per §14.1, but invariant #3 is "in any timezone" and the config copy has none | **FIXED** 2026-09-01 (JC-43) |
| N8 | `metrics` internal | `stratum_weighted_incremental_recovery` rejects a compared stratum missing from the weights but not a weight naming an uncompared stratum. One extra ₹9,000 stratum moved `per_rupee_delta` 0.400000 → 0.040000 and `total_at_risk` ₹1,000 → ₹10,000 | `point` survives (the dilution cancels) but the headline is then reported over a denominator it never measured — JC-35's whole subject | **DEFERRED** — before eval-freeze |
| N9 | `metrics` internal | `human_minutes` exists on both `ArmOutcome` and its `cost`, unreconciled (999 vs 12.5 accepted); both are unquantised `Decimal`, so `12.5` and `12.50` hash differently | Two §13 rows read different fields, and it is the only field in the layer that breaks JC-15's byte-identical canonical form | **DEFERRED** — before eval-freeze |
| N10 | `experiment` internal | `verify_assignment` checks `experiment_id` and `salt_digest` but ignores `stratum_definition_version` and `assignment_version`, which both models carry | The verifier written to catch "a forgotten re-run" reports success across a stratum-definition change (JC-02) | **DEFERRED** — before eval-freeze |
| N11 | `enums` vs `case` vs `spine/tables` | Two definitions of terminal — `TERMINAL_CASE_STATES` and `not ALLOWED_CASE_TRANSITIONS[state]`. They agree today; no test says so. The DB's one-live-case index uses the first, `RiskCase.is_terminal` the second | If they diverge, the database and the model disagree about which cases are live | **DEFERRED** — no trigger |
| N12 | `actions` internal | §6-1's guard checks that `channel_field` names a real field; nothing checks the mirror, `is_outbound_contact=True` implies `channel_field is not None` | §6-1 failed loudly; this direction fails silently — the gate reads `None` and skips the channel | **DEFERRED** — no trigger |
| N13 | `events` internal | `CanonicalEvent.trust` defaults to `SYSTEM_OF_RECORD`, the highest level, and the envelope/payload trust check runs in one direction only | The only fail-**open** default in a layer that fails closed everywhere else | **DEFERRED** — no trigger |
| N14 | `events` internal | No `ingested_at >= occurred_at` check, though the docstring says confusing the two "produces a timing model trained on our own latency" | Every other temporal pair in the layer is ordered | **DEFERRED** — no trigger |
| N15 | `audit` internal | `tool_call.case_id` is compared to `row.case_id` only when the latter is non-None, so a `schedule_debit` envelope on a `case_id=None` row is accepted | Invariant #8 wants one action, one row; an unattributable money-moving row satisfies the schema | **DEFERRED** — no trigger |
| N16 | `audit` internal | `verify_chain` checks sequence, `prev_hash` and `row_hash` but not `ts` monotonicity — a chain whose row 1 predates row 0 by 30 days returns `is_valid=True` | JC-27's argument is that the chain survives a hostile reader; §15 replay cannot order it by time | **DEFERRED** — no trigger |
| N17 | eight registries | Each names a consumer that does not exist: `ARM_SPECS` ("enforced by a contract test" — no test imports it), `ID_PREFIXES` ("used by contract tests and the audit log's reference validator" — neither exists), `SystemicIncident.counts_toward_at_risk` ("so the metrics module can assert it" — never asserted), `INCIDENT_AT_RISK_IS_NOT_ADDITIVE`, `MetricDirection` ("used by the guardrail check" — which hardcodes `> 0`), `EVENT_PAYLOAD_UNION_MEMBERS`, `AUDIT_EVENT_TYPES`, `HEADLINE_CONTROL_ARM`/`HEADLINE_TREATMENT_ARM` | **This is the pattern, not an instance.** Each reads as a live cross-check, so the next contributor has no reason to add one | **DEFERRED** — **recheck on every new registry consumer** (below) |
| N18 | `actions` internal | `idempotency_scope` says "each override is pinned by a test"; there are zero overrides | Describes a mechanism that has never run, so Phase 1's first override lands on an untested path | **DEFERRED** — no trigger |
| N19 | `experiment` vs `spine/case_machine` | `preregistration_digest` hardcodes its computed-field exclude list; `case_machine._rebuild` derives the same set from `model_computed_fields` | Adding a computed field to `ExperimentSpec` silently changes the digest JC-41 says pins the run | **DEFERRED** — no trigger |
| N20 | `case` vs `policy_format` | `ABSTENTION_CONFIDENCE_CEILING` (0.5) and `diagnosis_confidence_floor` (0.55) must stay ordered for JC-25 to hold, and nothing asserts it | Lower the floor in YAML and an `UNKNOWN` diagnosis at 0.45 becomes actionable | **DEFERRED** — no trigger |
| N21 | `enums` internal | `InboundIntent.triggers_hard_stop` docstring says "dispute / opt_out"; the code returns three, including `HARDSHIP` | **The code is right** (§14.3 lists hardship) — the docstring under-states it, so a reader checking the comment concludes wrongly | **DEFERRED** — no trigger |
| N22 | `metrics` internal | `guardrail_contract_holds` checks only the treatment arm's `policy_violations` | A violation in the control arm passes the guardrail | **DEFERRED** — no trigger |
| N23 | frozen-model discipline | `PolicyThresholds.max_grace_period_days_by_segment`, `RuleTestCase.facts` and `ExperimentSpec.arm_weights_permille` are `Mapping` (mutable dicts) inside `frozen=True` models where the layer otherwise uses `tuple`; `RuleTestCase.facts` values are `Any`, unchecked against `FACT_TYPES` | A rule's own allow/deny cases — §14.1's "an untested rule cannot be loaded" — are the one place fact types go unvalidated | **DEFERRED** — no trigger |

### Trigger points

- ~~**Before any policy-engine work: N6 first, then N1, N2, N7.**~~ **All four done,
  2026-09-01** — written up below. The policy-engine gate is clear; nothing in §7
  now blocks writing the rule set.
- **Before eval-freeze (Phase 4): N8, N9, N10.** All three move a published number
  rather than a decision, so they can wait — but not past the run that publishes it.
- **N11–N23: no near-term trigger.** Real, verified, and cheap to fix later.

### N17 is a recheck, not a task

The other twenty-two are defects. N17 is the mechanism that produced them, so it is
not fixed once. **Whenever a registry gains its first real consumer — a test that
walks it, or Phase 1 code that reads it — recheck the other seven in the same pass**,
because the comment claiming a consumer is precisely what stops anyone noticing there
isn't one. `ARM_SPECS` is the sharpest instance: `simulation_only=True` is the flag
that keeps A5 off real rails, its comment says a contract test enforces it, and no
test imports `ARM_SPECS` at all.

### What was fixed in this pass

N3, N4 and N5, all in `obligations.py` — the consent/hold/payment-matching half of
the module, which had **zero test coverage** through Phase 0. That is exactly why
three free-string fields with closed vocabularies written in their own descriptions
survived 322 green tests. Each fix was driven by a test watched to fail first; one of
those tests passed *vacuously* on its first run (an empty `get_args` loop over a
`str` annotation) and was tightened before any production code was written.

- `PartialPayment.match_method` is now the same `Literal` as
  `PaymentReceivedPayload.match_method`, and the test re-derives the event's
  vocabulary rather than restating it, so the two cannot drift apart again.
- `ConsentProfile` refuses more than one record per channel. Withdrawal is already
  modelled *inside* a record, so a second row was never a history — it was a
  contradiction with a tie-break.
- `Hold.kind` is a `StopReason` restricted to the new `enums.HOLD_STOP_REASONS`, the
  seven §14.1 holds. Both the set and its complement are walked: the nine
  `StopReason` members that stop a *ladder* rather than the payer are refused, so a
  hold cannot suppress contact that policy still permits.

`CONTRACTS_SCHEMA_VERSION` 1.1.0 → **2.0.0**. All three narrow what parses, and
`Hold.kind` changes value as well as type (`'opt_out'` → `'hard_stop_opt_out'`),
which is MAJOR by `versions.py`'s own rule. Nothing persisted predates it.
`PHASE_0_FROZEN` is untouched and still `False`.

### The policy-engine amendment — N1, N2, N6, N7 (2026-09-01)

The four rows the trigger list put ahead of any policy-engine work, done as one
scoped pass. `enums.py` was opened and not changed: `PolicyCategory` already had
both categories N6 needed. Suite 380 → **407**.

Each new test was then run against the tree with *its own* fix reverted, one fix at
a time — twelve probes, all of which failed as required. One did not: a test
asserting that `quiet_hours` rejects a string passed on the pre-amendment tree too,
because a non-optional field rejected strings already. It was rewritten to assert
the annotation itself (`{QuietHours, NoneType}`) before it counted for anything.
That is the §7 `get_args` lesson landing a second time, and it is the reason the
probe exists at all rather than a claim that the tests are good.

**N1 — the mapping is guarded twice, because an import-time guard alone is theatre
here.** `DEFAULT_MAX_GRACE_PERIOD_DAYS_BY_SEGMENT` is now a module-level table with
the same import-time totality check as `FACT_TYPES`, `RAIL_SPECS`, `ACTION_SPECS`,
`PAYLOAD_MODELS` and `DECLINE_CLASS_META`. But that guard can only ever see the
*default*, and N1's actual risk is a YAML file that omits a segment — so
`PolicyThresholds._coherent` rejects a non-total mapping on every instance. Tests
walk the enum and drop each segment individually, so a guard that only notices an
empty mapping fails.

**N2 — both missing branches, plus the `in` form.** `FactPredicate` now requires a
timezone-aware `datetime` for TIMESTAMP facts (`quiet_hours_end_at gte 5` was
comparing an instant against the scalar 5) and a real member of the fact's own enum
for ENUM facts, via a new `ENUM_FACT_VOCABULARIES` table walked in both directions
at import. **Scope extended deliberately:** the `IN`/`NOT_IN` path returned early
before any type check, so `rail in ("card_emandat",)` — the natural way to write a
multi-rail rule — would have stayed exactly as unchecked as before. Closing N2 on
the `eq` form only would have been closing it on the rarer spelling.

**N6 — one category added, one deliberately refused.** `financial_authority` is now
on `schedule_debit`: its `guard_summary` promised "T2 above the AFA threshold" and
`policy_categories` is what a generic engine iterates, so the one money-moving verb
in the catalog was asking for no financial-authority rule at all. **`timing` was
not added, and the N6 row above is wrong to list it.** §14.1 files the ≥24h
pre-debit notification under **Rail / network** — already present, and the lead
time is a rail mechanic cited in `rails.py`, not a threshold we choose. Every rule
in §14.1's Timing row (quiet hours, declared holidays, contact windows) is scoped
to *contact*, and a mandate-authorised debit is not contact. Adding `timing` would
have made a debit consult quiet hours, which is a different product. A test now
pins `TIMING ∈ policy_categories ⟺ requires_quiet_hours_check` across the whole
catalog, so the claim is walked rather than argued.

**N7 — the precedence rule, and the field change it required (JC-43).** A payer's
stated window wins in their own zone; absent one, the configured window read as
`FALLBACK_QUIET_HOURS_TIMEZONE` = Asia/Kolkata. `resolve_quiet_hours` in
`policy_format.py` is the only place that is decided. Two consequences worth
reading as costs rather than features:

- `ConsentProfile.quiet_hours` is now `QuietHours | None`, default `None`. While it
  defaulted to 09:00–19:00 IST there was no way to tell "this payer told us their
  hours" from "this payer told us nothing", so the configured window could never
  legitimately apply to anybody and the second source was decorative. This is the
  one break in the amendment that does **not** announce itself: a 2.x payload
  omitting the field still parses, to a different value.
- `PolicyThresholds` now refuses a quiet-hours boundary that is not on the hour.
  `QuietHours` speaks in whole local hours, so a configured `09:30` would have been
  truncated to `09:00` by the fallback and silently widened the window we may
  contact in. **This is scope I chose, not scope N7 asked for** — but the
  alternative was opening a new silent-widening path in the act of closing a
  precedence hole.

`resolve_quiet_hours` returns the *window*, not a yes/no. Answering "may I contact
now?" needs a clock, and §12.5.4 keeps clocks out of this layer. **The limit that
follows is real: this makes one place to decide the window, not one place to do the
timezone arithmetic.** A Phase 1 engine can still convert in the wrong zone, and
can still read `ConsentProfile.quiet_hours` directly — the field description says
not to, and a field description is prose, which is the category of thing §7 is a
list of.

**Versions.** `CONTRACTS_SCHEMA_VERSION` 2.0.0 → **3.0.0** (the `quiet_hours` type
and default both change; N1 and N2 narrow what parses). `POLICY_FORMAT_VERSION`
1.0.0 → **2.0.0** (a rule set 1.0.0 would have loaded may now fail to).
`ACTION_CATALOG_VERSION` 1.0.0 → **1.1.0** (N6 is additive; the idempotency scope is
`{case_id, params}` and does not carry the constant, so no stored envelope's key
moves). Both of the last two are §11.4-sensitive and both are safe to bump *now*
precisely because the `SEED_EVAL` run has not happened. `PHASE_0_FROZEN` is
untouched and still `False`.

**What this pass did not touch.** N8/N9/N10 keep their eval-freeze trigger;
N11–N23 keep theirs. N17's recheck **did** apply — `ENUM_FACT_VOCABULARIES` is a new
registry, and it ships with the test that walks it in the same change, which is the
whole point of the recheck.


---

## 8. Tracked, not urgent — three stale claims (noticed 2026-09-02)

Found while wiring the Phase 1 policy engine and the end-to-end flow
(`reclaim/policy/`, `reclaim/flow.py`). **None of them is fixed here**, deliberately:
each is a documentation claim rather than a defect, and editing prose in the same
change as behaviour is how a stale claim becomes an unreviewed one. Logged so they
are not lost.

| # | Where | The claim | Why it is now false |
|---|---|---|---|
| S1 | §4, and §5's "Strengths" | "No persistence, no I/O of any kind"; "251 tests" | `reclaim/spine/` has been a SQLAlchemy schema, four tables and a database engine since Phase 1 began. §4 is still true *of `reclaim/contracts/`* — and `test_contract_hygiene.py` still enforces exactly that — but the sentence reads as a claim about the repository. §5's 251 is the freeze-era count; the suite is 489. |
| S2 | `CLAUDE.md` | "full suite (251 tests)"; "There are no detectors, no policy rules, … no persistence, no I/O" | Same drift, one file over, and worse: `CLAUDE.md` is what a fresh session reads first, so it is the claim most likely to be believed. There are now policy rules (`reclaim/policy/rules.py`, sixteen of them), a policy engine, a deterministic router and a wired flow. The layering, invariants and conventions sections remain accurate. |
| S3 | This document's own header, line 13 | `python -m pytest` as the "verify the tree yourself" snippet | Q9 says every command in this repo needs `PYTHONIOENCODING=utf-8` because `Money.__str__` emits `₹` and Windows `cp1252` raises on it. The snippet a reviewer is invited to paste is the one command in the document that does not carry it — and a failing rupee sign is the first thing they would see. |

**Why not just fix them.** S1 and S2 need a decision, not an edit: the honest
replacement for "no persistence" is a sentence about *which layer* has none, and
that sentence should be written when Phase 1's shape settles rather than three
times as it changes. S3 is a one-word fix, but it belongs with whatever change
finally settles the documented commands (Q9 also proposes an ASCII fallback in
`verify_chain`, which would make the environment variable unnecessary rather than
mandatory — and those two answers should not both ship).

**Cost of leaving them.** A reader trusts §4 and `CLAUDE.md` about scope; both now
overstate the freeze's purity, which is the direction that matters — a claim that
understates would merely be modest. S3 costs a reviewer one confusing crash.
