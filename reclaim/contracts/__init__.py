"""The frozen Phase 0 contracts.

Deliberately empty of re-exports. Every consumer imports from the module that owns
the symbol -- ``from reclaim.contracts.actions import ActionEnvelope``, not
``from reclaim.contracts import ActionEnvelope``. Two reasons:

1. A package-level re-export makes ``import reclaim.contracts`` pull in the whole
   catalog, the policy format, the metrics table and the experiment spec. That
   turns any import cycle introduced in Phase 1 into an unexplainable failure at
   interpreter start rather than a clear one at the offending line.
2. The import line names the owning module, so a reviewer reading a Phase 1 file
   can see which contract a symbol belongs to without a lookup.

The dependency layering between these modules, which no Phase 1 code may invert.
It is acyclic today; the check is three lines of ``ast`` and belongs in CI::

    L0  canonical  enums  ids  money  temporal  units  versions
    L1  decline_taxonomy  rails  obligations
    L2  strata  events  policy_format  actions
    L3  metrics  case  audit
    L4  experiment
"""
