"""RECLAIM Phase 1 -- the policy engine.

``reclaim.contracts.policy_format`` froze the *shape* of a rule and the lattice
that combines verdicts, and shipped no rules and no evaluator, on purpose. This
package is the other half:

* ``rules``     -- the minimal, real rule set, built from ``PolicyThresholds`` so
                   the AFA threshold stays a config value (§11.2).
* ``facts``     -- the closed fact vocabulary, assembled from state the model
                   cannot influence: the ledger, the obligation, the consent
                   record, the outbox, the clock.
* ``engine``    -- evaluation. Which rules an action is subject to, what a
                   missing fact means, and when to refuse to answer.
* ``templates`` -- the small template registry the content rules read.

Like ``reclaim.contracts``, this package's ``__init__`` re-exports nothing:
import from the owning module. Unlike ``reclaim.contracts``, nothing here is
forbidden to touch the clock -- ``facts`` exists precisely to do the timezone
arithmetic §12.5.4 keeps out of the contract layer. It still does no I/O: the
caller reads the database and hands the results in.

Deliberately absent, and each is a stated gap rather than an oversight (see
``rules.NOT_YET_ENCODED``): autonomy tiers (§14.2), earned autonomy (§14.5), a
contact-history store, a reconciliation freshness check, and an approval queue.
"""
