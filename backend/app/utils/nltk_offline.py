"""One rule for "may this process reach the network for NLTK data?" (issue #491).

NLTK corpora are fetched **at runtime, on first use**, from inside the
transcription and topic pipelines. On an airgapped or firewalled deployment those
calls do not fail fast — ``nltk.download`` swallows its own network errors and
returns falsy, so the call either hangs on a socket timeout or returns quietly and
the caller discovers the corpus is still missing one line later.

The codebase already has exactly this convention for Hugging Face:
``HF_HUB_OFFLINE=1``, set by ``docker-compose.offline.yml`` on every backend
service and honoured at ``core/celery.py`` and ``transcription/diarizer.py``. NLTK
simply never got the same treatment, so the offline overlay asserted an
environment the NLTK call sites could not see.

``NLTK_OFFLINE=1`` is that assertion. When it is set, the download sites skip the
network entirely and say **which setup step was skipped** rather than timing out
against a host that was never going to answer. It does not make anything work that
would not otherwise work — it turns a slow, silent degradation into a fast,
attributable one.

⚠️ **This is a guard, not a fallback.** Every caller still needs its own
degradation path for the case where the corpus is genuinely absent and offline was
never declared; see ``text_preprocessing._get_stopwords`` and
``search/chunking_service`` for the two shapes (degrade with a warning, and latch
the regex splitter for the process).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Mirrors ``HF_HUB_OFFLINE``: the string ``"1"``, nothing else. A truthy-string
#: check would make ``NLTK_OFFLINE=0`` mean offline, which is the opposite of what
#: an operator typing it intends.
_OFFLINE_VALUE = "1"

#: Every way NLTK can fail to hand back a usable corpus. Catch **this**, not
#: ``LookupError`` alone, in a degradation path.
#:
#: ``LookupError`` is the resource-MISSING case, and it is the only one NLTK
#: documents — so every degradation path in this repo was written against it.
#: That is a category error: "the corpus is absent" and "the corpus is present
#: but unreadable" are the same event to a caller, and only one of them was
#: being handled.
#:
#: ``OSError`` is the resource-PRESENT-BUT-UNREADABLE case. It covers wrong
#: ownership on the model cache (the exact condition
#: ``scripts/fix-model-permissions.sh`` exists to repair), a truncated or
#: corrupt pickle, a full or unreadable volume — and, since nltk 3.10, its
#: **pathsec** hardening against CWE-59, which raises ``PermissionError`` (an
#: ``OSError`` subclass) for a corpus file whose ``st_nlink`` exceeds one:
#:
#:     Security Violation [pathsec.open]: refusing multiply-linked file
#:     '…/punkt_tab/english/collocations.tab' (st_nlink=3)
#:
#: That is not hypothetical. It escaped the ``except LookupError`` in
#: ``segment_dedup.split_sentences_nltk`` and **failed the whole transcription**
#: — the one outcome the guard was added (issue #491) to prevent. NLTK is not in
#: the ASR or diarization path; it powers sentence splitting, chunking and topic
#: extraction, all of which have working degraded modes. A transcript that is
#: merely coarser is strictly better than no transcript at all.
#:
#: Deliberately NOT ``Exception``: a ``TypeError`` or ``AttributeError`` from
#: this codebase is a defect and must keep propagating. These two branches are
#: the only ways a *data file* fails to load.
NLTK_CORPUS_UNAVAILABLE = (LookupError, OSError)


def nltk_offline() -> bool:
    """Whether this deployment has declared itself airgapped for NLTK."""
    return os.getenv("NLTK_OFFLINE") == _OFFLINE_VALUE


def nltk_downloads_permitted(*, corpus: str = "NLTK data") -> bool:
    """Whether a caller may attempt ``nltk.download``.

    Args:
        corpus: Named in the log line so the operator learns *which* corpus is
            missing, not merely that something is.

    Returns:
        False when ``NLTK_OFFLINE=1``, having logged what to run instead.
    """
    if not nltk_offline():
        return True

    logger.warning(
        "NLTK_OFFLINE=1: refusing to download %s. This deployment declared itself "
        "airgapped, so the corpora must be provisioned at setup time — run "
        "scripts/download-models.sh, or ./opentr.sh start (which calls it). See "
        "docs-site/docs/installation/offline-installation.md.",
        corpus,
    )
    return False
