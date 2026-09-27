"""Log-safe descriptions of LLM prompt/response text (issue #1022).

Model output is derived from the transcript it was given — quotes, names, topics —
so writing it to the application log copies transcript content into log
aggregation and error trackers, which have different retention and access rules
from the database. At INFO and above, log only :func:`describe_llm_text`: a length
plus a short stable hash, enough to tell whether two failures saw the same
response without disclosing what it said.

:func:`log_llm_text_excerpt` emits an excerpt at DEBUG only. The shipped logging
config (``core/logging_config.py``) pins the root logger at INFO, so an excerpt
reaches a log only when a developer deliberately lowers the level while
debugging a model's output format.
"""

import hashlib
import logging

_HASH_CHARS = 12
_DEFAULT_EXCERPT_CHARS = 500


def describe_llm_text(text: str | bytes | None) -> str:
    """Return ``len=<n> sha256=<12 hex>`` for ``text``, never the text itself.

    Args:
        text: The prompt or response text. ``None`` is described as such.

    Returns:
        A string safe to log at any level.
    """
    if text is None:
        return "len=0 sha256=none"
    data = text if isinstance(text, bytes) else text.encode("utf-8", errors="replace")
    digest = hashlib.sha256(data).hexdigest()[:_HASH_CHARS]
    return f"len={len(text)} sha256={digest}"


def log_llm_text_excerpt(
    log: logging.Logger,
    label: str,
    text: str | bytes | None,
    limit: int = _DEFAULT_EXCERPT_CHARS,
) -> None:
    """Log the first ``limit`` characters of ``text`` at DEBUG, and never higher.

    Args:
        log: The caller's module logger.
        label: What the text is, e.g. ``"topic extraction response"``.
        text: The prompt or response text.
        limit: Maximum number of characters to include.
    """
    if text is None or not log.isEnabledFor(logging.DEBUG):
        return
    log.debug("%s excerpt (%s): %r", label, describe_llm_text(text), text[:limit])
