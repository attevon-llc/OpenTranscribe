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

**The two tasks scope differently, and the asymmetry is deliberate.**

``propagate_title_rename`` covers the **whole file plane**: a digest inherits
``title`` from ``base_metadata``, ``chunk_retrieval`` reads it when building a
digest ``ChunkHit``, and ``chat/citations`` renders it — so a chunk-plane-only
rewrite let one answer cite the same recording under two different names. The
title is metadata, not derived prose, so rewriting it leaves the document
internally consistent.

``propagate_speaker_rename`` covers the **chunk plane only**. A digest has no
``speaker`` field (``build_digest_documents`` pops it, keeping digests out of the
speaker facet and out of chat's speaker-scoped ``terms`` filter), and its prose
bakes the name in a way ``update_by_query`` cannot reach. Rewriting the roster
alone would produce a half-corrected document and disguise the fact that
regeneration is the real fix.

Both tasks **re-resolve the target from Postgres at run time** rather than
trusting the value captured at dispatch. Two renames of one speaker (``A -> B``
then ``B -> C``) are independent tasks on an 8-way queue with no ordering: if
``B -> C`` runs first it matches nothing and succeeds, then ``A -> B`` writes
**B**, and chat's exact ``terms`` filter on C returns zero chunks — #405's own
bug, recreated by #405's dispatch model. Re-reading makes both orderings
converge.

⚠️ **The digest PROSE still bakes the old speaker name and none of this fixes
that.** Rewriting the roster keeps the filterable fields honest; regenerating the
prose is the #383 addendum-G1 trigger, whose designated hook is ``_finish``
below — one place, not scattered across the dispatch sites. Until it lands, a
renamed speaker's digest text is stale until the file is reindexed.
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
  // SORTED, because the indexer sorts (search_indexing_task: `speaker_names =
  // sorted({...})`, issue #455 — an unsorted roster changed the EMBEDDINGS). The
  // rebuild above preserves positional order, so ["Bob","Zed"] with Bob->Zoe
  // became ["Zoe","Zed"] and the document stopped being what a reindex would
  // produce. Java's String.compareTo and Python's sorted() agree on ordinary
  // text; they can differ only above the BMP, which no display name here reaches.
  Collections.sort(rebuilt);
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


def _current_speaker_name(speaker_id: int) -> str | None:
    """The name the chunk plane SHOULD carry for *speaker_id*, read at run time.

    Trusting the ``new_name`` captured at dispatch is what makes two renames
    invert. ``A -> B`` and ``B -> C`` are independent tasks on an 8-way queue with
    no ordering: if ``B -> C`` runs first it matches nothing and succeeds, then
    ``A -> B`` writes **B**. Postgres then says C, the index says B, and chat's
    exact ``terms`` filter on C returns zero chunks — #405's own bug, recreated by
    #405's dispatch model.

    Re-reading here makes execution order irrelevant: whichever task runs, it
    writes the name Postgres holds now, so both orderings converge on C.

    Mirrors the indexer's own rule — ``display_name or name``
    (``search_indexing_task``) — so the value written is the value a reindex
    would write.

    Returns:
        The current name, or ``None`` when the speaker is gone (deleted between
        dispatch and execution) or the lookup failed. Callers fall back to the
        dispatched name rather than skipping the rewrite: a stale rewrite is
        better than a stale index.
    """
    try:
        from app.db.session_utils import session_scope
        from app.models.media import Speaker

        with session_scope() as db:
            row = (
                db.query(Speaker.display_name, Speaker.name)
                .filter(Speaker.id == speaker_id)
                .first()
            )
    except Exception as exc:  # noqa: BLE001 — fall back to the dispatched name
        logger.warning(f"Could not re-resolve speaker {speaker_id} at run time: {exc}")
        return None

    if not row:
        return None
    display_name, name = row
    return str(display_name or name or "") or None


def _current_file_title(file_uuid: str) -> str | None:
    """The title the chunk plane SHOULD carry for *file_uuid*, read at run time.

    Same reasoning as :func:`_current_speaker_name`: two quick renames dispatch
    two tasks with no ordering guarantee, and the loser would otherwise write the
    superseded title. Mirrors the indexer's rule,
    ``media_file.title or media_file.filename``.
    """
    try:
        from app.db.session_utils import session_scope
        from app.models.media import MediaFile

        with session_scope() as db:
            row = (
                db.query(MediaFile.title, MediaFile.filename)
                .filter(MediaFile.uuid == file_uuid)
                .first()
            )
    except Exception as exc:  # noqa: BLE001 — fall back to the dispatched title
        logger.warning(f"Could not re-resolve title for file {file_uuid} at run time: {exc}")
        return None

    if not row:
        return None
    title, filename = row
    return str(title or filename or "") or None


def _retry_on_conflicts(task: Any, response: dict[str, Any], what: str, file_uuid: str) -> bool:
    """Retry when ``conflicts="proceed"`` skipped documents. Returns True if retrying.

    ``proceed`` means "do not abort the whole update_by_query on a version
    conflict" — it does NOT mean the skipped documents were handled. Nothing
    re-examines them, so a concurrent title+speaker rename over the same file left
    a **subset** of its chunks carrying the old value while the task reported
    ``status: success``.

    Reading the count and retrying is what makes the declared ``max_retries`` real
    rather than decorative. Combined with the run-time re-resolution above, a
    retry converges instead of racing the same writer again.
    """
    conflicts = int(response.get("version_conflicts", 0) or 0)
    if not conflicts:
        return False

    logger.warning(
        f"{what} for file {file_uuid} hit {conflicts} version conflict(s); "
        f"retrying (attempt {task.request.retries + 1}/{task.max_retries})"
    )
    try:
        task.retry(countdown=task.default_retry_delay)
    except task.MaxRetriesExceededError:
        logger.error(
            f"{what} for file {file_uuid} still had {conflicts} version conflict(s) after "
            f"{task.max_retries} retries; those documents keep the old value until a reindex"
        )
        return False
    return True


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
    bind=True,
    name="propagate_speaker_rename",
    priority=CPUPriority.USER_TRIGGERED,
    max_retries=3,
    default_retry_delay=10,
)
def propagate_speaker_rename(
    self: Any,
    file_uuid: str,
    old_names: list[str],
    new_name: str,
    speaker_id: int | None = None,
) -> dict[str, Any]:
    """Rewrite ``speaker`` / ``speakers`` on one file's documents after a rename.

    ⚠️ **This rewrites the display/keyword fields ONLY — the vector keeps the old
    roster**, exactly as :func:`propagate_title_rename` does for the title, and
    for the same reason: ``indexing_service`` bakes the roster into
    ``embedding_text`` (``build_embedding_text(..., roster=...)``), so a renamed
    speaker still participates in semantic matching under the old name until the
    file is reindexed. The trade is deliberate — re-embedding every chunk of a
    long recording for a rename costs hundreds of model calls, and the things a
    user notices immediately (the facet, the citation, chat's speaker scope) are
    what this fixes. The pipeline is applied per bulk action rather than as an
    index ``default_pipeline``, so ``update_by_query`` does **not** silently
    re-embed: the vector goes stale rather than being recomputed from stale text,
    which is the safe half. If a rename ever needs to move the vector, dispatch a
    reindex rather than widening this script.

    Args:
        file_uuid: UUID of the media file whose documents carry the stale name.
        old_names: The names the documents were indexed with. A list because one
            file can hold several diarized speakers that a batch accept collapses
            onto a single person.
        new_name: The display name as of dispatch. Used only as a fallback —
            *speaker_id* is re-resolved at run time when supplied, because the
            dispatched value is what makes two quick renames invert.
        speaker_id: Postgres id of the renamed speaker, when the dispatcher knows
            it. Optional so a task queued by an older build still runs.

    Returns:
        Dict with update stats.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search.indexing_service import chunk_plane_query

    # Re-resolve before computing `stale`: the current name may be one of the
    # names we were told to replace (A->B then B->C, executed out of order).
    if speaker_id is not None:
        current = _current_speaker_name(int(speaker_id))
        if current and current != new_name:
            logger.info(
                f"Speaker {speaker_id} is now '{current}', not the dispatched "
                f"'{new_name}'; writing the current name"
            )
            # The dispatched name is itself stale now, so it joins the names to
            # replace — otherwise the chunks an earlier task already rewrote to
            # it would keep it forever.
            old_names = [*(old_names or []), new_name]
            new_name = current

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
    #
    # ⚠️ CHUNK plane only, deliberately — unlike `propagate_title_rename`, which
    # covers the whole file plane. A digest carries a `speakers` roster but no
    # `speaker` field at all (`build_digest_documents` pops it, so a digest stays
    # out of the speaker facet and out of chat's speaker-scoped `terms` filter),
    # and its PROSE bakes the old name in a way no `update_by_query` can rewrite.
    # Updating the roster while the prose still says "SPEAKER_01" would produce a
    # half-corrected document and hide the fact that regeneration is what this
    # actually needs — the #383 addendum-G1 trigger, hooked at `_finish`.
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
    if _retry_on_conflicts(self, response, "Speaker rename propagation", file_uuid):
        return {"status": "retrying", "file_uuid": file_uuid, "updated": updated}
    logger.info(
        f"Speaker rename propagated for file {file_uuid}: "
        f"{updated} document(s) {stale} -> '{new_name}'"
    )
    return {
        "status": "success",
        "file_uuid": file_uuid,
        "updated": updated,
        "version_conflicts": int(response.get("version_conflicts", 0) or 0),
    }


@celery_app.task(
    bind=True,
    name="propagate_title_rename",
    priority=CPUPriority.USER_TRIGGERED,
    max_retries=3,
    default_retry_delay=10,
)
def propagate_title_rename(self: Any, file_uuid: str, new_title: str) -> dict[str, Any]:
    """Rewrite ``title`` on every document of one file after it was renamed.

    ``update_transcript_title`` only touches the full-document transcript index;
    the chunk plane feeds search result cards and chat citations, both of which
    would keep showing the old title.

    ⚠️ **This rewrites the display/keyword field ONLY — the vector keeps the old
    title.** ``indexing_service`` bakes the title into ``embedding_text``
    (``build_embedding_text(title=..., recorded_at=..., roster=..., body=...)``),
    so the title is not decoration: it participates in semantic matching, and a
    chunk from "Q3 Pricing Review" embeds differently from the same words under
    "Untitled recording". The script below sets ``ctx._source.title`` and nothing
    else, so after a rename **semantic** search still matches on the old title
    until the file is reindexed.

    That is a deliberate trade, not an oversight: re-embedding every chunk of a
    long recording for a cosmetic rename costs hundreds of model calls, and the
    two things a user notices immediately — the card and the citation — are the
    ones this fixes. If a rename ever needs to move the vector too, dispatch a
    reindex rather than widening this script; ``embedding_text`` is derived at
    index time and cannot be patched correctly in place.

    Args:
        file_uuid: UUID of the media file that was renamed.
        new_title: The current title.

    Returns:
        Dict with update stats.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search.indexing_service import file_plane_query

    if not file_uuid or not new_title:
        return {"status": "skipped", "reason": "nothing_to_rename", "updated": 0}

    # Same run-time re-resolution as the speaker path: two quick renames dispatch
    # two unordered tasks, and the loser would otherwise write the superseded title.
    current = _current_file_title(file_uuid)
    if current and current != new_title:
        logger.info(
            f"File {file_uuid} is now titled '{current}', not the dispatched "
            f"'{new_title}'; writing the current title"
        )
        new_title = current

    client = get_opensearch_client()
    if not client:
        logger.warning("OpenSearch client not available, skipping title rename propagation")
        return {"status": "skipped", "reason": "no_opensearch", "updated": 0}

    index_name = settings.OPENSEARCH_CHUNKS_INDEX
    try:
        response = client.update_by_query(
            index=index_name,
            # `file_plane_query`, not `chunk_plane_query`: digest documents inherit
            # `title` from `base_metadata` and `chat/chunk_retrieval` reads it when
            # building a digest ChunkHit. Scoping to the chunk plane meant one
            # answer could cite the same recording under two different names —
            # the new title from a chunk, the old one from a digest.
            body={
                "query": file_plane_query(file_uuid),
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
    if _retry_on_conflicts(self, response, "Title rename propagation", file_uuid):
        return {"status": "retrying", "file_uuid": file_uuid, "updated": updated}
    logger.info(f"Title rename propagated for file {file_uuid}: {updated} document(s)")
    return {
        "status": "success",
        "file_uuid": file_uuid,
        "updated": updated,
        "version_conflicts": int(response.get("version_conflicts", 0) or 0),
    }


def dispatch_speaker_rename(
    renames: Iterable[tuple[str | None, str | None]],
    new_name: str,
    speaker_id: int | None = None,
) -> int:
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
        speaker_id: Postgres id of the renamed speaker, when there is a single
            one. Passed to the task so it can re-resolve the current name at run
            time and converge regardless of execution order. Omitted for a
            profile-wide rename, which sweeps many speakers at once and therefore
            has no single id to re-read.

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
                file_uuid=file_uuid,
                old_names=old_names,
                new_name=new_name,
                speaker_id=speaker_id,
            )
        except Exception as exc:  # noqa: BLE001 — dispatch failure must not break the rename
            logger.warning(f"Could not queue speaker rename propagation for {file_uuid}: {exc}")
    return len(by_file)
