"""Wait out a background PII warm-up so it cannot outlive the test that started it."""

from __future__ import annotations

import sys


def join_inflight_pii_warmup(timeout: float = 120) -> bool:
    """Join ``app.services.redaction.warmup._warmup_thread`` if one is alive.

    Reads the module only if already imported, so tests that never touch redaction do not
    pay an import. Returns True if it had to wait on a live thread. Fails if the thread is still
    running after ``timeout`` seconds.
    """
    warmup = sys.modules.get("app.services.redaction.warmup")
    thread = getattr(warmup, "_warmup_thread", None)
    if thread is None or not thread.is_alive():
        return False
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "a PII warm-up started by this test is still building"
    return True
