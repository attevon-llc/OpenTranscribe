#!/usr/bin/env python3
"""Find DB sessions held open across slow non-DB work.

This is the single most repeated defect in this codebase, and it has wedged the live
database twice in one day on two different workers. A task that wraps its body in one
``with session_scope() as db:`` and then runs ffmpeg, a MinIO transfer, an OpenSearch
round trip, an LLM completion, an SMTP send or a model load **inside** it holds a
Postgres transaction for the whole duration. A plain SELECT takes ACCESS SHARE for the
life of its transaction, so such a hold:

1. **queues every ``ALTER TABLE``** — i.e. it hangs an Alembic upgrade mid-release, and
   dev runs migrations automatically on backend startup;
2. **pins the VACUUM horizon** on ``transcript_segment``, the largest table in the product;
3. **consumes a pool connection** for as long as it lives.

Measured: a CPU worker ``idle in transaction`` for 48 minutes (a 3-hour task killed by the
hard time limit) and an NLP worker for 1 h 26 m. Both were found only because DDL migration
tests started failing with ``psycopg2.errors.LockNotAvailable`` and the suite went
138.9 s → 224.8 s.

The rule and the three-phase fix pattern live in ``backend/app/tasks/CLAUDE.md``;
``backend/app/tasks/speaker_attribute_task.py`` is the worked example.

Detectors
    session-subprocess
        ``subprocess.run``/``Popen``, or one of this repo's ffmpeg wrappers, inside a session
        body. An ffmpeg transcode is minutes, and ``timeout=300`` bounds the process, not
        the transaction.
    session-object-storage
        A MinIO / S3 / boto transfer inside a session body. Uploads and downloads here are
        gigabytes over the network.
    session-search-engine
        An OpenSearch round trip inside a session body. Individually fast, but these appear
        in per-item loops: 50 re-index calls or 2N profile writes in ONE transaction.
    session-http
        ``requests`` / ``httpx`` / ``aiohttp`` / ``urllib`` inside a session body. An outbound
        request is bounded only by its own timeout — and often has none.
    session-llm
        An LLM completion inside a session body. Multi-minute over a whole transcript, and a
        provider stall is bounded only by the HTTP timeout. This is what held the NLP worker's
        transaction for 1 h 26 m.
    session-model
        A model load or an inference pass inside a session body. A cold wav2vec2/Whisper load
        is 40-60 s before any work starts.
    session-smtp
        An SMTP / Graph mail send inside a session body — 30 s of timeout **per recipient
        config**, spent holding the cluster-wide vacuum horizon for nothing.
    session-thread-pool
        A ``ThreadPoolExecutor``/``ProcessPoolExecutor`` block inside a session body.
        ``__exit__`` calls ``shutdown(wait=True)`` with **no timeout**, so a per-future
        ``result(timeout=30)`` is decorative: one wedged child holds the block, and the
        transaction, indefinitely.
    session-param-slow-work
        **The interprocedural rule.** A function that both ACCEPTS a ``Session`` and performs
        slow work. The caller's ``with session_scope()`` is then one frame up, where no
        body-scan can see it — which is exactly how ``watch_source.scan_single`` hid: its
        session wrapped ``_perform_scan(db, ...)``, and the remote listing, the per-file
        download and the MinIO upload were all a frame further down. Passing ``db`` into slow
        work is also how the idiom SPREADS; the fix is to take the data, or open a short
        session inside.

Usage::

    scripts/audit-session-lifetime.py backend/app
    scripts/audit-session-lifetime.py backend/app --json
    scripts/audit-session-lifetime.py backend/app --category session-llm
    scripts/audit-session-lifetime.py --selftest     # audit the auditor (no tree needed)

Exits 1 when any finding is not in the allowlist, so this can gate a commit. The allowlist is
``scripts/session-lifetime-allowlist.txt``: one ``<file>::<scope>::<category>  # reason`` per
line, all three segments required and the reason mandatory. The category is part of the key on
purpose — an entry keyed by function alone would exempt it from every detector at once.

**The allowlist is COUNT-AWARE and can only SHRINK.** One line buys one finding, duplicate
keys encode a count, and an entry with no finding left to cover FAILS the run. Fix a call,
delete its line. A reason starting ``BACKLOG`` marks deferred work and is counted and printed
separately, so a green gate is never read as a clean tree.

**Run ``--selftest`` after touching any detector.** A detector that matches nothing reports
zero findings, which is indistinguishable from a clean codebase — this repo's most repeated
failure mode, and the reason four separate gates were found silently not working. Every
detector has a must-fire case AND the clean cases (the real three-phase fix shapes) must stay
silent. ``backend/tests/unit/test_session_lifetime_audit.py`` runs the same cases under
pytest, so the suite fails if a detector goes blind even when nobody remembers the flag.
"""

from __future__ import annotations

import sys

from session_lifetime_audit.cli import main

if __name__ == '__main__':
    sys.exit(main(__doc__ or ''))
