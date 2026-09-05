# RECLAIM

**An AI revenue-recovery agent for failed payments — built so that every number it
reports can be checked, and every action it takes can be refused.**

Hackathon Track 03.

---

## The problem in one paragraph

A subscription debit fails. A B2B invoice goes past due. Today someone either
ignores it or blasts the customer with the same reminder sequence regardless of
*why* the payment failed. But "insufficient funds at 2am on the 1st" and "the
card's authentication token expired" and "this customer is trying to leave" are
three completely different problems, and only one of them is fixed by a reminder.
Meanwhile every message you send is regulated — consent, quiet hours, contact
caps, mandate rules — so the cost of guessing wrong is not just a wasted send.

**RECLAIM diagnoses the failure, decides whether contacting this person is even
allowed, takes the narrowest action that fits, and then measures — against a
control group — whether any of it actually recovered money.**

---

## Run it

```bash
pip install -e ".[dev]"

python demo.py          # the whole thing, end to end, ~10 seconds
python -m pytest        # 696 tests
```

That's it. No API keys, no database to set up, no environment variables. The demo
uses an in-memory SQLite database and writes nothing to disk.

`python demo.py` exits **0** if the audit chain verifies and no runtime invariant
was breached, and **1** otherwise — so it works as a CI gate, not just a printout.

Useful flags:

| Flag | What it does |
|---|---|
| `--cases 2000` | bigger batch (default 200) |
| `--allocation planned` | draw into all six experiment arms, not just the three that are implemented |
| `--basis resolved_only` | per-protocol instead of intent-to-treat (**flatters the agent** — see below) |
| `--resamples 1000` | faster bootstrap; anything under 10,000 is reported as not publishable |

---

## What the demo shows you, stage by stage

### 1 · Seeding the batch
200 obligation-cases — failed card debits and overdue B2B invoices — randomly
assigned to experiment arms. Assignment is `sha256` over a frozen salt, **never
Python's builtin `hash()`**, which is randomised per-process and would silently
split cases differently between your run and ours.

### 2 · Running the agent
Each case goes through the real pipeline:

```
raw PSP decline code       "insufficient_funds"  "expired_card"  "processing_error"
        │
        ▼  normalize/        one of 24 canonical decline classes
        │
        ▼  diagnosis/        which of 9 root-cause hypotheses (H1–H9), + a confidence
        │
        ▼  flow.route        the narrowest action that fixes THAT cause
        │
        ▼  policy/           16 rules across 8 gates: consent, quiet hours, frequency,
        │                    rail, content, financial limits, holds, integrity
        │
        ▼  spine/outbox      ALLOW → queued.  DENY → stopped.  ESCALATE → human.
```

The normalizer **raises on a decline code it doesn't know** rather than falling back
to a default class. An unrecognised code becoming "insufficient funds" by default is
how you end up retrying a cancelled mandate forever.

You'll see something like:

```
     34  denied
     22  allowed
     10  pending_approval

Compliance denials, by the rule that decided them (§14.1):
     15  POL-TIME-001        (quiet hours in the payer's timezone)
     15  POL-CONSENT-001     (no consent for this purpose on this channel)
      4  POL-HOLDS-001       (dispute / hardship / bereavement / legal hold)
```

**The denials are the point, not a failure.** A revenue agent that cannot be told
"no" is not deployable at any recovery rate.

### 3 · Simulating the payer response
There is no public labelled dataset of *decline code → intervention → outcome*, so
recovery probabilities come from `reclaim/sim/anchors.py` — assumptions reasoned
from published industry ranges. Each draw is `sha256(salt | case_id | arm)`, so the
same case cannot carry good luck from one arm into another.

### 4 · Scoreboard
The headline: net incremental recovery with a bootstrap 95% confidence interval at
10,000 resamples, against two controls.

### 5 · Verifying the audit chain
Every decision is a row in a hash-chained log. The verifier re-computes all ~1,350
rows and confirms none was edited, reordered or removed from the middle. It also
tells you what it *cannot* prove (truncation from the end) and how to pin that too.

### 6 · Checking the runtime invariants
Ten safety properties — no double debit, no contact after opt-out, recovered never
exceeds owed — checked against what actually landed in the database.

---

## The result, stated plainly

Here is the default run's headline:

```
  arm                                         cases  recov    rate  per Re
  A0  no action (natural recovery)               49     13   0.265   0.278
  A1  fixed schedule + static 4-touch drip       85     36   0.424   0.467
  A4  full agent: diagnose -> policy -> act      66     20   0.303   0.283

  A4 vs A0   net incremental recovery = ₹0.37 L   (95% CI −₹20.90 L .. ₹20.88 L)  p = 0.96
  A4 vs A1   net incremental recovery = −₹13.36 L (95% CI −₹32.12 L .. ₹5.89 L)   p = 0.17
```

**Read that carefully: the agent currently loses to a dumb static drip.**

We are telling you this in the README rather than burying it, because the reason is
specific and measured, not mysterious:

> **The agent only acts on ~33% of its cases. The drip acts on 100%.**
>
> A1 sends everyone four messages and ignores consent and quiet hours entirely — it
> is an §14.1-violating baseline. A4 obeys the rules, so 34 of its 66 cases get
> denied or parked. A4's *per-contact* uplift is roughly three times A1's; it just
> gets to contact far fewer people. Multiply a better intervention by a third of the
> population and you lose.

Neither comparison is statistically significant (p = 0.96 and p = 0.17), and both
confidence intervals comfortably span zero. **We are not claiming the agent works.**
What we are claiming is that the measurement is honest enough to show us that it
doesn't yet — which is the part most hackathon scoreboards quietly skip.

### What would fix it, in order of size

1. **A scheduler.** 15 of the denials are quiet-hours denials — cases that are
   perfectly contactable four hours later. The policy engine can already express
   `DEFER`; the rule set doesn't use it, because there is nowhere to put a deferred
   action. This is engineering, not a new assumption.
2. **A real consent store.** The other 15 denials come from
   `flow.stand_in_consent_profile`, which fabricates consent from arithmetic on the
   payer id (`n % 9`, `n % 7`, `n % 11` ≈ 34% denial by construction). **A made-up
   input is currently the single largest determinant of the headline.**
3. **An LLM diagnostician.** The one place §9.2 says a model genuinely belongs is
   the ambiguous-authorization class. But the measurement says diagnosis is *not*
   the binding constraint right now — compliance coverage is — so this is third,
   not first. We reordered this after measuring; it used to be first.

---

## What is real and what is simulated

Being wrong about this distinction is how a demo becomes a lie, so:

| Component | Status |
|---|---|
| Decline-code normalizer, diagnostician, router | **Real.** Deterministic, tested. |
| Policy engine + 16 rules across 8 gates | **Real.** Every action passes through it. |
| State machine, outbox, hash-chained audit log | **Real.** SQLite/Postgres, verifiable. |
| Metrics, bootstrap CI, invariant checker | **Real.** |
| **Payer/PSP recovery outcomes** | **Simulated.** Assumptions, not observations. |
| **Consent profiles and holds** | **Stand-ins.** Fabricated from the payer id. |
| Message delivery | **Not built.** Actions are enqueued, never sent. |
| Cost to collect | **Not modelled.** Every figure is gross, not net. |
| LLM calls | **None anywhere.** The agent is fully deterministic. |

The absolute recovery rates above are therefore properties of our simulator, not of
any real portfolio. **The only claim they support is the comparison between arms
inside this one environment** — which is randomised, seeded and reproducible.

---

## Five design decisions worth two minutes

These are the things that would be expensive to retrofit and cheap to get right up
front, so we got them right up front.

**1 · Money is an integer number of paise. Always.**
No float ever touches money. `Money * float` raises. `Decimal` is an input type,
never storage. The canonical JSON serialiser **rejects a float at any depth** and
names the path where it found one, so a rounding error cannot reach an audit row.

**2 · Hashes are derived, never supplied.**
An action's idempotency key and an audit row's hash are computed from the row's own
contents. A stored row with an edited amount doesn't merely fail verification —
**it fails to parse.** The audit chain also hashes the sequence number, because a
plain `prev_hash` chain cannot detect that you deleted the last ten rows.

**3 · The action catalog is closed.**
Exactly 13 write verbs. Nine more are named as forbidden and asserted disjoint at
import time. Every action model rejects unknown fields and **has no free-text
field** — `send_message` has no `body`, only named slots in a registered template.
So the answer to "what if the model is fully compromised?" is structural: a
compromised planner still cannot invent a verb, write prose to a customer, or move
money outside a mandate.

**4 · Untrusted input is untrusted in the type system.**
An inbound customer message forces an untrusted envelope type. You cannot
accidentally treat it as a trusted fact.

**5 · The simulator and the agent cannot see each other.**
A test walks the import graph and fails if any agent module imports
`reclaim.sim`, or if the simulator imports the router, diagnostician or policy
engine. Otherwise the agent could condition on the hidden outcome table and the
experiment would be measuring itself. *(This is why `demo.py` lives outside
`reclaim/` — it is the one file that legitimately imports both.)*

---

## Repo map

```
demo.py                     the runner — start here
reclaim/
  contracts/                Phase 0: frozen schemas, enums, pure functions.
                            No I/O, no LLM, no filesystem — enforced by tests.
  normalize/                raw PSP decline code  ->  canonical class
  diagnosis/                canonical class       ->  root cause + confidence
  policy/                   compliance rules, facts, message templates
  spine/                    database, state machine, outbox, audit log, seeding
  sim/                      simulated payer responses + the scoreboard
  flow.py                   the orchestrator that wires the above together
  invariants.py             §14.6's ten runtime safety checks
tests/                      696 tests
HACKATHON_PLAN.md           the spec. Code cites it as §N.
CONTRACTS.md                what was frozen, the 9 open questions, the review log
```

**Reading order if you have ten minutes:** `demo.py` → `reclaim/flow.py` →
`reclaim/policy/rules.py` → `reclaim/invariants.py`.

---

## Tests

```bash
python -m pytest                              # all 696
python -m pytest tests/test_flow.py -q        # one file
python -m ruff check                          # clean
```

Two conventions in here are deliberate and worth knowing before you edit:

- **Tests re-derive results independently** rather than asking the module. The arm
  assignment test recomputes the hash from the documented recipe and runs the
  assigner in subprocesses under three different `PYTHONHASHSEED` values. A test
  that calls the implementation to compute its own expectation cannot catch a
  silent re-randomisation of 2,000 cases.
- **A table is frozen only when a test walks every row.** Four of five defects found
  in an adversarial review of already-green code lived in mappings that had been
  checked by eye and spot-tested by member.

> **Note on Windows:** the demo and the test suite both work with no environment
> setup. But an ad-hoc one-liner that prints money (`python -c "...print(Money...)"`)
> will still hit `UnicodeEncodeError`, because the Windows console defaults to
> `cp1252` and can't encode `₹`. Prefix such commands with `PYTHONIOENCODING=utf-8`.

---

## Honest limitations

Beyond the simulated/stand-in components already listed:

- **No LLM.** The diagnostician is deterministic. Arm A4 is therefore not a test of
  "does an LLM help" — that comparison (A4 − A3) exists in the plan and **cannot be
  computed from this code**, so we don't quote it.
- **Six of ten runtime invariants report `not_checkable`.** There is no consent
  store, holds table, mandate table or notification log in this schema, so those
  checks have nothing to run against. The checker refuses to call them green — an
  unverifiable invariant is deliberately not a pass, and the demo prints all six
  rather than reporting "4/4". Reporting "10/10 green" would have been easy and
  would have been false.
- **Estimates are pooled, not stratum-weighted.** §12.1 asks for a stratum-weighted
  estimator, but at n=200 almost no amount-band × failure-class × segment cell is
  populated in *both* arms, so `strata_count = 1` on every estimate.
- **Costs are zero**, so "net" incremental recovery equals gross. Adding real costs
  moves the arms unequally — A1 sends four times A4's contacts; A4 spends human
  approval minutes A1 never does.
- **Arms A2, A3 and A5 are not implemented.** A T-12h scope cut kept A0, A1 and A4.
  The default `--allocation eval` switches the others off explicitly at 0 permille
  rather than pretending they were never in the design.
- **The ceiling on this design, honestly:** with the scheduler and a real consent
  store, the agent's action coverage goes from ~33% toward ~90%, and its per-contact
  advantage over the drip would then apply to a comparable population. That is the
  point at which the A4 − A1 comparison becomes worth running properly. We have
  deliberately not converted that into a rupee projection — an earlier projection
  here was wrong by a factor of ten, and we would rather ship the arithmetic than
  the forecast.
