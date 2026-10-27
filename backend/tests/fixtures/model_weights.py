"""Decide whether a ``models`` test may SKIP for a missing model, or must FAIL.

The redaction ``models`` tests used to ``pytest.skip`` whenever a detector's ``preload()``
returned false. That conflates two different situations:

* the weights are not on this machine -- a skip is honest, nothing could be measured;
* the weights ARE here and the load still failed -- a library upgrade (transformers 5,
  spaCy, presidio) broke the load, and production silently has no toxicity scoring / no PII
  detection. A skip there hides exactly the regression the test exists to catch.

``require_loaded`` makes the second case a failure. "Here" is decided by the same resolver the
app uses (``huggingface_hub``'s cache lookup, offline) or, for a pip-installed spaCy model, by
whether the package is importable.
"""

from __future__ import annotations

import importlib.util

import pytest


def hf_weights_cached(repo_id: str) -> bool:
    """True when a snapshot of ``repo_id`` is already in the local Hugging Face cache."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        snapshot_download(repo_id, local_files_only=True)
    except LocalEntryNotFoundError:
        return False
    return True


def package_installed(name: str) -> bool:
    """True when ``name`` is importable (e.g. a spaCy model distributed as a wheel)."""
    return importlib.util.find_spec(name) is not None


def require_loaded(loaded: bool, *, available: bool, what: str) -> None:
    """Skip when the model is genuinely absent; FAIL when it is present but did not load.

    Args:
        loaded: Whether the detector's ``preload()`` succeeded.
        available: Whether the weights / package are present on this machine.
        what: Human description used in the message.
    """
    if loaded:
        return
    if available:
        pytest.fail(
            f"{what} is present on this machine but failed to load. This is a regression "
            "(library/model incompatibility), not a missing-weights skip; check the "
            "detector's logger.error output for the cause."
        )
    pytest.skip(f"{what} unavailable on this machine (weights/package not installed)")
