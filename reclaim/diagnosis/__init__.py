"""Diagnosis: root-cause inference for a ``RiskCase``.

Phase 0 froze the ``Diagnosis`` schema (``reclaim.contracts.case``). This package
is where diagnoses are *produced*. Only the deterministic fallback path exists so
far (``deterministic``): no LLM, no tool loop, no evidence gathering. It is the
path §9.1 takes on "LLM fail / timeout", and the whole diagnosis behaviour behind
arm A3 ("deterministic diagnosis -> intervention routing").

Import from the owning module: ``from reclaim.diagnosis.deterministic import
diagnose``.
"""
