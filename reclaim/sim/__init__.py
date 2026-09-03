"""RECLAIM's **simulated environment**. Nothing in here is a real payment rail.

This package is the answer to a question the rest of the repository cannot answer
about itself: *did the money arrive?* The spine records what the agent proposed and
what policy allowed; it has no executor and no PSP, so every case in the ledger
stops at ``scheduled`` with the outcome unknown. This package supplies the outcome,
by drawing it from a documented probability table -- and it is a **simulator**, so
every number it produces is environment-dependent by construction.

§12.5.4 item 4 requires the environment to be a separate module the agent cannot
reach into, and ``tests/test_sim_outcomes.py`` asserts exactly that: no module under
``reclaim/`` outside this package imports ``reclaim.sim``. The dependency runs one
way -- the simulator reads the spine's public API, the spine knows nothing about the
simulator. Put an import of ``reclaim.sim`` into ``flow.py`` and the build fails,
because at that point a detector or a router could read the hidden outcome table and
the whole experiment would be measuring itself.

* ``anchors``  -- the §11.2 calibration constants. Pure Decimals and tables, no I/O.
                  Every one is an **assumption with an uncertainty band**, not a
                  measurement; ``ANCHOR_HONESTY_NOTE`` is the sentence that belongs
                  on the slide next to any number derived from them.
* ``outcomes`` -- the resolver. Reads a post-flow case out of the ledger, draws its
                  outcome deterministically from ``case_id``, walks §9.1's state
                  machine to the state that outcome implies, and writes one
                  ``simulated_psp_response`` audit row saying so in those words.

Scope, per the T-12h cut (§18.4): **A0, A1 and A4 only.** A2, A3 and A5 are not
simulated at all -- their cases are returned as ``NOT_SIMULATED`` rather than given a
number, because a made-up number for an arm nobody built is worse than a gap.

Like ``reclaim.contracts`` and ``reclaim.policy``, this ``__init__`` re-exports
nothing: import from the owning module.
"""
