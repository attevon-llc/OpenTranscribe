"""Erasure receipt (GDPR Art. 17 / Art. 30): what an erasure removed, per store, and proof that
nothing is left (issue #1353).

``inventory`` lists every store an erasure touches and how to count it, ``capture`` names the
subject's identifiers BEFORE the destroy, ``measure`` takes a measurement in a fresh session,
``receipt`` turns two measurements into the persisted, numbers-only record. Plan:
``docs/design/erasure_receipt_plan.md``.
"""
