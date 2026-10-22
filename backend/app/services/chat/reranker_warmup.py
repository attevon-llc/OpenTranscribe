"""Load the chat reranker at API startup instead of on the first chat (issue #1269).

``reranker.get_reranker`` is lazy, so the first chat turn in every fresh API process
paid the whole cold load inside its retrieval stage. Measured in the dev backend
container (fresh process, host load average 59-90 on 48 cores, 6 runs): 9.9-12.3 s for
``get_reranker()``, nearly all of it importing ``sentence_transformers``/torch and
building the ``CrossEncoder``. The cloud report was ~15 s. The same work moved to a
daemon thread started by the lifespan costs the lifespan only ``Thread.start()``.

This is a scheduling change and nothing else. It calls the same
:func:`~app.services.chat.reranker.get_reranker` with the same model name, so the
object a chat turn gets is the one it would have loaded itself: same weights, same
``device="cpu"``, same ``max_length``. Retrieval order cannot change.

Constraints, each of which rules out an obvious alternative:

- **Startup must not block.** The settings read and the load both run on the thread;
  the backend healthcheck and readiness never wait for it.
- **Load once.** ``get_reranker`` holds a lock with a re-check, so a chat that arrives
  mid-warm-up waits for the one load already in progress rather than starting a second.
- **A failed warm-up must not start the load-failure cooldown.** ``get_reranker``
  normally backs off for ``RETRY_COOLDOWN_S`` after a failure. If the warm-up started
  that cooldown, a model cache mounted a few seconds after the API came up would make
  every chat for five minutes skip reranking, which is worse than not warming at all.
  The warm-up passes ``start_cooldown_on_failure=False``, so the first real chat still
  makes its own attempt, exactly as it did before this module existed.
- **Only where chat runs, and only what chat uses.** The lifespan is the only caller,
  and it runs only in the ASGI process: Celery workers import ``app.main`` without
  running it, and ``TESTING=true`` returns before any startup task. Skipped when the
  admin has reranking off (``chat.rag.rerank_enabled``), because no turn would use the
  model, and when the ``chat.rag.rerank_warmup_enabled`` row is false (the opt-out;
  default :data:`~app.core.constants.DEFAULT_CHAT_RAG_RERANK_WARMUP_ENABLED`). Only the
  English model is loaded. The multilingual arm is used only for a predominantly
  non-English pool and only when its own flag is on; its first such turn still pays
  its own load.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from app.core import constants as C  # noqa: N812

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: ``SystemSettings`` key for the opt-out. Deliberately not a ``CHAT_FLAG_REGISTRY``
#: entry: it is read once per process at startup, so an admin-panel toggle would
#: suggest an effect that only appears after a restart.
WARMUP_SETTING_KEY = "chat.rag.rerank_warmup_enabled"

_PROBE_PAIR = ("what was decided about the budget?", "We agreed to hold the budget flat.")

_warmup_thread: threading.Thread | None = None

_SessionFactory = Callable[[], AbstractContextManager["Session"]]


def warm_reranker() -> bool:
    """Load the default reranker and score one throwaway pair.

    Returns:
        True if the model is loaded afterwards. False if the load failed; that is
        logged by ``get_reranker`` and never raised.
    """
    from app.services.chat import reranker

    started = time.perf_counter()
    model = reranker.get_reranker(C.CHAT_RERANKER_MODEL, start_cooldown_on_failure=False)
    if model is None:
        logger.warning(
            "Chat reranker warm-up did not load the model; the first chat will try "
            "again itself, as it did before warm-up existed"
        )
        return False

    try:
        model.predict([_PROBE_PAIR])
    except Exception:  # noqa: BLE001 — a warm-up must never be load-bearing
        logger.warning("Chat reranker loaded, but the warm-up probe failed", exc_info=True)

    logger.info("Chat reranker warmed in %.2f s", time.perf_counter() - started)
    return True


def warmup_wanted(session_factory: _SessionFactory | None = None) -> bool:
    """Whether this deployment's settings call for a warm-up. Reads the DB once.

    Args:
        session_factory: Context-manager factory yielding a session; defaults to
            ``session_scope``. Tests pass their own.

    Returns:
        False when reranking is off, the opt-out row is false, or the settings
        cannot be read. A failed read skips the warm-up: the lazy path still works.
    """
    from app.services.chat.settings import get_chat_settings
    from app.services.system_settings_service import get_setting_bool

    if session_factory is None:
        from app.db.session_utils import session_scope

        session_factory = session_scope

    try:
        with session_factory() as db:
            enabled = get_setting_bool(
                db, WARMUP_SETTING_KEY, C.DEFAULT_CHAT_RAG_RERANK_WARMUP_ENABLED
            )
            rerank_enabled = get_chat_settings(db).rerank_enabled
    except Exception:  # noqa: BLE001 — a warm-up decision must never surface
        logger.warning("Could not decide whether to warm the chat reranker", exc_info=True)
        return False

    if not enabled:
        logger.info("Chat reranker warm-up skipped: %s is false", WARMUP_SETTING_KEY)
        return False
    if not rerank_enabled:
        logger.info("Chat reranker warm-up skipped: chat.rag.rerank_enabled is false")
        return False
    return True


def _warm_if_wanted(session_factory: _SessionFactory | None = None) -> None:
    """Thread body. The settings session is closed before the load starts."""
    try:
        if warmup_wanted(session_factory):
            warm_reranker()
    except Exception:  # noqa: BLE001 — an optimisation must not raise anywhere
        logger.warning("Chat reranker warm-up failed", exc_info=True)


def start_reranker_warmup(session_factory: _SessionFactory | None = None) -> threading.Thread:
    """Start the background warm-up. Returns the thread, or the one already running.

    Args:
        session_factory: Passed to :func:`warmup_wanted`; ``None`` in production.

    A daemon thread rather than an asyncio task or ``run_in_threadpool``: the load is
    CPU-bound and cannot be cancelled, and a threadpool slot would be taken from
    request handling for its whole duration.
    """
    global _warmup_thread
    running = _warmup_thread
    if running is not None and running.is_alive():
        return running
    thread = threading.Thread(
        target=_warm_if_wanted,
        args=(session_factory,),
        name="chat-reranker-warmup",
        daemon=True,
    )
    _warmup_thread = thread
    thread.start()
    return thread
