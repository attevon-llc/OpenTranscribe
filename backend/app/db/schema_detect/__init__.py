"""Fingerprint an untracked database's schema to the Alembic revision to stamp it at.

One module per era of the migration chain, each holding that era's probes and its slice of
the newest-first ladder; ``ladder.py`` runs every probe and orders the slices.
"""
