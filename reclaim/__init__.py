"""RECLAIM -- an AI revenue-recovery agent (Track 03).

Phase 0 of the build is a *contract freeze*: everything under
``reclaim.contracts`` is a schema, an enum, or a pure function, with no I/O, no
network calls, and no dependency on anything outside ``pydantic`` and the standard
library. Phase 1's detectors, policy rules and UI import from here and may not
redefine any of it.

See ``CONTRACTS.md`` at the repository root for the decisions these modules encode
and for the places ``HACKATHON_PLAN.md`` left open.
"""

__all__ = ["contracts"]
