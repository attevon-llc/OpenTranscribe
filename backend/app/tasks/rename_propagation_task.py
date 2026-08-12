"""Propagate speaker and title renames into ``transcript_chunks`` (issue #405).

Chunk documents store a **snapshot** of the speaker display name (``speaker``,
plus the file-level ``speakers`` array) and the file title (``title``) taken at
indexing time. Renaming a speaker or a file used to update only the speaker /
voiceprint indices and the full-document transcript index, so the chunk plane
kept the old value until somebody ran a full reindex.

That is a correctness bug, not a cosmetic one. Chat's speaker axis resolves
display names from **Postgres** (the current name) and then filters the chunk
index with an exact ``terms`` match on ``speaker``
(``hybrid_search_service._build_filters``). Rename ``SPEAKER_01`` to ``Dana``
and every chunk indexed before the rename becomes unreachable under the only
name the user can ask about — the model then answers confidently from whatever
is left. Search facets are built from the index too, so the dropdown *offers*
the stale name, and citations attribute quotes to it.

These tasks follow the precedent set by ``search_indexing_task``'s
``update_file_access_index`` / ``update_file_tags_index``: a targeted
``update_by_query`` on the chunk plane, ``conflicts="proceed"``, one refresh at
the end. The predicate is built by
``services.search.indexing_service.chunk_plane_query`` so the ``doc_type``
discriminator #383 Phase 3 adds reaches these rewrites too.

Both tasks bump the chat corpus version when they change anything. Skipping that
makes the fix *look* broken: chat would keep serving cached retrievals carrying
the old name for the length of the cache TTL.

**Seam for #383 Phase 3 (addendum G1):** once per-file digests bake speaker
names into prose, a rename must also retrigger digest generation. Both tasks
return ``updated`` counts and already know the ``file_uuid``, so the trigger
belongs in ``_finish`` below — one place, not scattered across the dispatch
sites.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from app.core.celery import celery_app
from app.core.constants import CPUPriority

logger = logging.getLogger(__name__)

# Painless, not a partial-doc update: ``speakers`` is an array that has to be
# rewritten element-wise, and a doc update cannot express "replace this one
# entry". ``ctx.op = 'noop'`` keeps a rename from rewriting (and re-embedding on
# a pipeline-backed index) chunks that never carried the old name.
#
# ``params.new`` would be a painless syntax error — ``new`` is a reserved word.
_SPEAKER_RENAME_SCRIPT = """
boolean changed = false;
if (ctx._source.speaker != null && params.old_names.contains(ctx._source.speaker)) {
  ctx._source.speaker = params.new_name;
  changed = true;
}
if (ctx._source.speakers instanceof List) {
  List rebuilt = new ArrayList();
  for (def name : ctx._source.speakers) {
    def mapped = params.old_names.contains(name) ? params.new_name : name;
    if (!rebuilt.contains(mapped)) { rebuilt.add(mapped); }
  }
  if (!rebuilt.equals(ctx._source.speakers)) {
    ctx._source.speakers = rebuilt;
    changed = true;
  }
}
if (!changed) { ctx.op = 'noop'; }
"""

_TITLE_RENAME_SCRIPT = """
if (ctx._source.title == params.title) {
  ctx.op = 'noop';
} else {
  ctx._source.title = params.title;
}
"""


def _finish(index_name: str, file_uuid: str, updated: int) -> None:
    """Make the rewrite visible and stop chat serving the pre-rename cache.

    Refresh first, then bump: a reader that misses the refresh and hits a cold
    cache would otherwise re-retrieve the old documents and cache *those*.
    """
    from app.services.opensearch_service import get_opensearch_client

    if not updated:
        return

    client = get_opensearch_client()
    if client:
        try:
            client.indices.refresh(index=index_name)
        except Exception as exc:  # noqa: BLE001 — visibility, not correctness of the write
            logger.warning(f"Chunk index refresh failed after rename of {file_uuid}: {exc}")

    # Importing the private helper deliberately: it is the ONE place that knows
    # how a corpus-version bump must fail (contained, never raising into the
    # caller), and a second copy here would be a second implementation of it.
    from app.services.search.indexing_service import _invalidate_chat_retrieval_cache

    _invalidate_chat_retrieval_cache()


@celery_app.task(
    name="propagate_speaker_rename",
    priority=CPUPriority.USER_TRIGGERED,
    max_retries=3,
    default_retry_delay=10,
)
def propagate_speaker_rename(file_uuid: str, old_names: list[str], new_name: str) -> dict[str, Any]:
    """Rewrite ``speaker`` / ``speakers`` on one file's chunks after a rename.

    Args:
        file_uuid: UUID of the media file whose chunks carry the stale name.
        old_names: The names the chunks were indexed with. A list because one
            file can hold several diarized speakers that a batch accept
            collapses onto a single person.
        new_name: The current display name.

    Returns:
        Dict with update stats.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search.indexing_service import chunk_plane_query

    stale = [name for name in dict.fromkeys(old_names or []) if name and name != new_name]
    if not file_uuid or not new_name or not stale:
        return {"status": "skipped", "reason": "nothing_to_rename", "updated": 0}

    client = get_opensearch_client()
    if not client:
        logger.warning("OpenSearch client not available, skipping speaker rename propagation")
        return {"status": "skipped", "reason": "no_opensearch", "updated": 0}

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    # Narrow to the docs that actually mention a stale name. ``speakers`` is
    # file-level, so a chunk spoken by somebody else still has to be rewritten —
    # hence the OR rather than a plain ``speaker`` term.
    query = chunk_plane_query(
        file_uuid,
        extra_filters=[
            {
                "bool": {
                    "should": [
                        {"terms": {"speaker": stale}},
                        {"terms": {"speakers": stale}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        ],
    )

    try:
        response = client.update_by_query(
            index=index_name,
            body={
                "query": query,
                "script": {
                    "source": _SPEAKER_RENAME_SCRIPT,
                    "lang": "painless",
                    "params": {"old_names": stale, "new_name": new_name},
                },
            },
            conflicts="proceed",
        )
    except Exception as exc:  # noqa: BLE001 — a rename must not 500 the caller
        logger.error(f"Speaker rename propagation failed for file {file_uuid}: {exc}")
        return {"status": "failed", "error": str(exc), "updated": 0}

    updated = int(response.get("updated", 0))
    _finish(index_name, file_uuid, updated)
    logger.info(
        f"Speaker rename propagated for file {file_uuid}: "
        f"{updated} chunk(s) {stale} -> '{new_name}'"
    )
    return {"status": "success", "file_uuid": file_uuid, "updated": updated}


@celery_app.task(
    name="propagate_title_rename",
    priority=CPUPriority.USER_TRIGGERED,
    max_retries=3,
    default_retry_delay=10,
)
def propagate_title_rename(file_uuid: str, new_title: str) -> dict[str, Any]:
    """Rewrite ``title`` on one file's chunks after the file was renamed.

    ``update_transcript_title`` only touches the full-document transcript index;
    the chunk plane feeds search result cards and chat citations, both of which
    would keep showing the old title.

    Args:
        file_uuid: UUID of the media file that was renamed.
        new_title: The current title.

    Returns:
        Dict with update stats.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search.indexing_service import chunk_plane_query

    if not file_uuid or not new_title:
        return {"status": "skipped", "reason": "nothing_to_rename", "updated": 0}

    client = get_opensearch_client()
    if not client:
        logger.warning("OpenSearch client not available, skipping title rename propagation")
        return {"status": "skipped", "reason": "no_opensearch", "updated": 0}

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    try:
        response = client.update_by_query(
            index=index_name,
            body={
                "query": chunk_plane_query(file_uuid),
                "script": {
                    "source": _TITLE_RENAME_SCRIPT,
                    "lang": "painless",
                    "params": {"title": new_title},
                },
            },
            conflicts="proceed",
        )
    except Exception as exc:  # noqa: BLE001 — a rename must not 500 the caller
        logger.error(f"Title rename propagation failed for file {file_uuid}: {exc}")
        return {"status": "failed", "error": str(exc), "updated": 0}

    updated = int(response.get("updated", 0))
    _finish(index_name, file_uuid, updated)
    logger.info(f"Title rename propagated for file {file_uuid}: {updated} chunk(s)")
    return {"status": "success", "file_uuid": file_uuid, "updated": updated}


def dispatch_speaker_rename(renames: Iterable[tuple[str | None, str | None]], new_name: str) -> int:
    """Queue chunk-plane propagation for a batch of ``(file_uuid, old_name)`` pairs.

    Coalesces per file so a batch accept that collapses four diarized speakers in
    one recording onto one person queues **one** task, not four — each of which
    would otherwise rewrite the same ``speakers`` array and lose to the next on
    version conflict.

    Every caller of a rename path goes through here rather than calling
    ``propagate_speaker_rename.delay`` directly, so "did this path propagate?" has
    one answer per path.

    Args:
        renames: ``(file_uuid, old_name)`` pairs. Entries that are incomplete or
            already carry ``new_name`` are dropped.
        new_name: The display name every listed speaker now has.

    Returns:
        Number of files a task was queued for.
    """
    if not new_name:
        return 0

    by_file: dict[str, list[str]] = {}
    for file_uuid, old_name in renames:
        if not file_uuid or not old_name or old_name == new_name:
            continue
        names = by_file.setdefault(str(file_uuid), [])
        if old_name not in names:
            names.append(str(old_name))

    for file_uuid, old_names in by_file.items():
        try:
            propagate_speaker_rename.delay(
                file_uuid=file_uuid, old_names=old_names, new_name=new_name
            )
        except Exception as exc:  # noqa: BLE001 — dispatch failure must not break the rename
            logger.warning(f"Could not queue speaker rename propagation for {file_uuid}: {exc}")
    return len(by_file)
