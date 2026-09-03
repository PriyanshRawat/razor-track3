"""Lookup tables that sit on top of the frozen contracts.

Nothing here is a schema or a domain model -- it is data (raw PSP strings ->
canonical vocabulary) plus the pure functions that read it. It lives outside
``reclaim.contracts`` for the same reason ``reclaim.spine`` does: a contract is
imported by everything and may not grow a table that a per-PSP quirk has to be
edited into.

Import from the owning module: ``from reclaim.normalize.decline_codes import
normalize_decline_code``.
"""
