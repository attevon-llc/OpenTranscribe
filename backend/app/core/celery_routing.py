"""Celery queue declarations and static task routing.

Pure data: no ``celery_app`` here (``core/celery.py`` imports this, never the reverse).
``TASK_ROUTES`` is installed as ``task_routes``; ``INTENTIONALLY_UNROUTED_TASKS`` is the
written-reason allowlist for tasks whose dispatch sites pick the queue at call time, read
by ``celery.unrouted_task_names()`` and ``tests/unit/test_task_routes_complete.py``.
"""

from kombu import Queue

from app.core.queues import CeleryQueues
from app.core.queues import gpu_preferred_queue

# Speaker/diarization work that PREFERS a GPU but produces a correct result on CPU
# (issue #865). All eight were pinned to 'gpu' — a queue `docker-compose.lite.yml`
# scales to zero consumers — so in lite mode every one of them was published into a
# queue nothing drains, with no error and the API already returned 200. The
# user-visible shape: a speaker reassignment reports success and the cross-file
# voiceprint update never happens.
#
# Named as a set so the family and its routing rule live in ONE place;
# `tests/unit/test_lite_mode_queue_consumers.py` reads it and asserts that in lite
# every routed queue actually has a live consumer, which no per-line route could be
# checked for. Local transcription is deliberately NOT here — see
# gpu_preferred_queue()'s docstring for why work that REQUIRES a GPU must stay put.
GPU_PREFERRED_TASKS: tuple[str, ...] = (
    "rediarize",
    "update_speaker_embedding_on_reassignment",
    "extract_v4_embeddings",
    "extract_v4_embeddings_batch",
    "speaker.recluster_all",
    "detect_speaker_attributes_batch",
    "analyze_speakers_combined_batch",
    "speaker_embedding_consistency_repair_batch",
)

# Resolved once here because `task_routes` is a static dict built at import time.
# Every dispatch site that needs it at CALL time (postprocess.py's explicit
# apply_async) calls gpu_preferred_queue() directly instead of importing this.
_GPU_PREFERRED_QUEUE = gpu_preferred_queue()

# Explicit queue declarations — single source of truth.
# With task_create_missing_queues=False, any typo in a queue name will raise
# an error at dispatch time instead of silently creating a phantom queue.
CELERY_QUEUES = tuple(Queue(q) for q in CeleryQueues.ALL)


TASK_ROUTES: dict[str, dict[str, str]] = {
    # Pipeline chain tasks (3-stage: CPU preprocess → GPU transcribe → CPU postprocess)
    # NOTE: "transcription.gpu_transcribe" is intentionally NOT listed here so
    # dispatch.py can route it to either "gpu" or "cloud-asr" at call time.
    # NOTE: "transcription.cpu_transcribe" is also intentionally NOT listed here
    # so dispatch.py can route it to "cpu-transcribe" at call time.
    "transcription.preprocess": {"queue": CeleryQueues.CPU},
    "transcription.postprocess": {"queue": CeleryQueues.CPU},
    "transcription.enrich_and_dispatch": {"queue": CeleryQueues.CPU},
    "transcription.pipeline_error": {"queue": CeleryQueues.UTILITY},
    "transcription.reconcile_cancellation": {"queue": CeleryQueues.UTILITY},
    # The speaker/diarization family that PREFERS a GPU but runs correctly
    # without one — 'gpu' in a full deployment, 'cpu' in lite, where nothing
    # consumes 'gpu' (issue #865). Listed once in GPU_PREFERRED_TASKS above so
    # the set and its routing rule cannot drift apart.
    **{name: {"queue": _GPU_PREFERRED_QUEUE} for name in GPU_PREFERRED_TASKS},
    "speaker.cluster_for_file": {"queue": CeleryQueues.CPU},
    # Download Queue - Network I/O tasks (concurrency=3, no GPU)
    "download.media_url": {"queue": CeleryQueues.DOWNLOAD},
    "download.media_playlist": {"queue": CeleryQueues.DOWNLOAD},
    "download.prepare_media": {"queue": CeleryQueues.DOWNLOAD},
    "download.prepare_bulk_subtitles": {"queue": CeleryQueues.DOWNLOAD},
    "download.prepare_transcript_document": {"queue": CeleryQueues.DOWNLOAD},
    "asr.download_local_model": {"queue": CeleryQueues.DOWNLOAD},
    # CPU Queue - CPU-intensive parallel tasks (concurrency=8, no GPU)
    "media.generate_waveform": {"queue": CeleryQueues.CPU},
    "media.generate_waveform_data": {"queue": CeleryQueues.CPU},
    "media.create_playback_rendition": {"queue": CeleryQueues.CPU},
    "analytics.analyze_transcript": {"queue": CeleryQueues.CPU},
    "detect_speaker_attributes": {"queue": CeleryQueues.CPU},
    "migrate_speaker_attributes": {"queue": CeleryQueues.CPU},
    # detect_speaker_attributes_batch / analyze_speakers_combined_batch are in
    # GPU_PREFERRED_TASKS above (they were pinned to 'gpu' here).
    "migrate_speakers_combined": {"queue": CeleryQueues.CPU},
    "system.update_gpu_stats": {"queue": CeleryQueues.CPU},
    "migrate_speaker_embeddings_to_v4": {"queue": CeleryQueues.CPU},
    "migration.normalize_embeddings": {"queue": CeleryQueues.CPU},
    "generate_thumbnail": {"queue": CeleryQueues.CPU},
    "migrate_thumbnails_to_webp": {"queue": CeleryQueues.CPU},
    "backfill_missing_thumbnails": {"queue": CeleryQueues.CPU},
    "reindex_transcripts": {"queue": CeleryQueues.CPU},
    "reindex_batch": {"queue": CeleryQueues.CPU},
    "search_index_maintenance": {"queue": CeleryQueues.CPU},
    "search_index_selfheal": {"queue": CeleryQueues.CPU},
    "index_file_summary": {"queue": CeleryQueues.CPU},
    # Bounded DB read plus a handful of dispatches; never needs a CPU/GPU slot.
    "search_index_sweep": {"queue": CeleryQueues.UTILITY},
    "search.reembed_degraded": {"queue": CeleryQueues.CPU},
    "neural_search_bootstrap": {"queue": CeleryQueues.UTILITY},
    "speaker_profile_opensearch_sync": {"queue": CeleryQueues.UTILITY},
    "opensearch_orphan_cleanup": {"queue": CeleryQueues.CPU},
    "storage_orphan_sweep": {"queue": CeleryQueues.CPU},
    # Irreversible deletion of voiceprints / inferred gender after a privacy switch is off
    # (issue #1280). A person asked for it, so it is not scheduled maintenance.
    "privacy.purge_voiceprints": {"queue": CeleryQueues.CPU},
    "privacy.label_resync": {"queue": CeleryQueues.CPU},
    "speaker_embedding_consistency_check": {"queue": CeleryQueues.CPU},
    # speaker_embedding_consistency_repair_batch is in GPU_PREFERRED_TASKS above.
    "process_speaker_update_background": {"queue": CeleryQueues.CPU},
    # Rename propagation into the chunk plane (issue #405). Lightweight
    # update_by_query, but user-visible latency matters (chat and search go
    # on answering with the old name until it lands), so it rides the CPU
    # queue next to the speaker update that triggers it rather than the
    # slower utility queue.
    "propagate_speaker_rename": {"queue": CeleryQueues.CPU},
    "propagate_title_rename": {"queue": CeleryQueues.CPU},
    # Digest-plane regeneration triggered by a rename (#383 addendum-G1).
    # Same queue and same reasoning as the two routes above: it is
    # dispatched from the same `dispatch_speaker_rename` call and a
    # renamed speaker's summary tier should not lag behind the roster
    # rewrite by much. A task with no route falls through to the default
    # 'celery' queue silently — see `_validate_task_routes` below.
    "regenerate_rename_digests": {"queue": CeleryQueues.CPU},
    "process_speaker_merge_background": {"queue": CeleryQueues.CPU},
    # CPU, not GPU: this task only extracts embeddings from already-known
    # segments (no diarization model pass) — SpeakerEmbeddingService resolves
    # its device via hardware_detection.py, so it runs fine on CPU. Lite mode
    # (docker-compose.lite.yml) scales the GPU worker to zero replicas, so
    # pinning this to 'gpu' left it queued forever there (issue #584).
    "extract_speaker_embeddings": {"queue": CeleryQueues.CPU},
    # One-off operator backfill (priority CPUPriority.MAINTENANCE).
    "tenant_backfill_organization_id": {"queue": CeleryQueues.CPU},
    # NLP Queue - LLM API calls (concurrency=4, no GPU needed)
    "ai.generate_summary": {"queue": CeleryQueues.NLP},
    "ai.identify_speakers": {"queue": CeleryQueues.NLP},
    "ai.extract_topics": {"queue": CeleryQueues.NLP},
    "ai.extract_topics_batch": {"queue": CeleryQueues.NLP},
    "ai.group_batch_files": {"queue": CeleryQueues.NLP},
    "ai.retroactive_auto_label": {"queue": CeleryQueues.NLP},
    "ai.auto_label_batch": {"queue": CeleryQueues.NLP},
    # Deterministic ingest artifacts (#383 Phase 2). No LLM and no model load — it
    # rides the nlp pool because that is the CPU-only enrichment pool, not because
    # it calls a provider. It must still run when none is configured (#403 D6).
    "artifacts.generate_file_facts": {"queue": CeleryQueues.NLP},
    # Redaction Queue - Content moderation detection (dedicated CPU service)
    "redaction.detect": {"queue": CeleryQueues.REDACTION},
    "redaction.reindex_all": {"queue": CeleryQueues.REDACTION},
    # Embedding Queue - Search indexing with embedding model (concurrency=1)
    "index_transcript_search": {"queue": CeleryQueues.EMBEDDING},
    # Partial OpenSearch field writes: no GPU or embedding model needed, so utility pool.
    "update_file_access_index": {"queue": CeleryQueues.UTILITY},
    "org_content_transfer.restamp": {"queue": CeleryQueues.UTILITY},
    "update_file_tags_index": {"queue": CeleryQueues.UTILITY},
    # Opt-in speaker_id/profile_id backfill maintenance pass (search_indexing_task.py) —
    # lightweight OpenSearch writes, same shape as its two siblings above.
    "backfill_speaker_id_fields": {"queue": CeleryQueues.UTILITY},
    # Utility Queue - Lightweight maintenance tasks (concurrency=8)
    "system.startup_recovery": {"queue": CeleryQueues.UTILITY},
    "system.recover_user_files": {"queue": CeleryQueues.UTILITY},
    "system.health_check": {"queue": CeleryQueues.UTILITY},
    "system.reclaim_lost_tasks": {"queue": CeleryQueues.UTILITY},
    "system.reclaim_orphaned_deliveries": {"queue": CeleryQueues.UTILITY},
    "cleanup_expired_files": {"queue": CeleryQueues.UTILITY},
    "files.purge_trash": {"queue": CeleryQueues.UTILITY},
    "files.purge_trash_now": {"queue": CeleryQueues.UTILITY},
    # The trash's index stamp converging (#1101): update_by_query calls only, no GPU/CPU work.
    "search.availability_reconcile": {"queue": CeleryQueues.UTILITY},
    "cleanup.run_periodic_cleanup": {"queue": CeleryQueues.UTILITY},
    "cleanup.deep_cleanup": {"queue": CeleryQueues.UTILITY},
    "cleanup.health_check": {"queue": CeleryQueues.UTILITY},
    "cleanup.emergency_recovery": {"queue": CeleryQueues.UTILITY},
    "cleanup.scratch_janitor": {"queue": CeleryQueues.CPU},
    "cleanup.orphan_upload_sweeper": {"queue": CeleryQueues.UTILITY},
    "check_migration_status": {"queue": CeleryQueues.UTILITY},
    "finalize_v4_migration": {"queue": CeleryQueues.UTILITY},
    "export_transcript_baseline": {"queue": CeleryQueues.UTILITY},
    "compare_transcript_baseline": {"queue": CeleryQueues.UTILITY},
    "imohash_recompute.recompute_all": {"queue": CeleryQueues.UTILITY},
    # Watch Sources (issue #26)
    "watch_source.scan_all": {"queue": CeleryQueues.UTILITY},
    "watch_source.scan_single": {"queue": CeleryQueues.DOWNLOAD},
    "watch_source.stitch_and_import": {"queue": CeleryQueues.CPU},
    "watch_source.send_notification": {"queue": CeleryQueues.UTILITY},
    "watch_source.cleanup_temp": {"queue": CeleryQueues.UTILITY},
    # Credential-bearing auth mail (reset / verification / invitation / security notice):
    # one SMTP or Graph round trip each, off the request path. Utility, never gpu.
    "auth_mail.password_reset": {"queue": CeleryQueues.UTILITY},
    "auth_mail.email_verification": {"queue": CeleryQueues.UTILITY},
    "auth_mail.invitation": {"queue": CeleryQueues.UTILITY},
    "auth_mail.security_notice": {"queue": CeleryQueues.UTILITY},
    # Scheduled database backups (Feature C)
    "backup.check_schedule": {"queue": CeleryQueues.UTILITY},
    "backup.run": {"queue": CeleryQueues.UTILITY},
    # Media mirror (issue #242): due-check is lightweight (utility); the run is
    # bulk object I/O → download queue (NEVER the gpu queue).
    "backup.mirror_check_schedule": {"queue": CeleryQueues.UTILITY},
    "backup.mirror_run": {"queue": CeleryQueues.DOWNLOAD},
    # Directory reconciliation / LDAP deprovisioning: LDAP searches + small DB
    # writes. CPU queue — never gpu.
    "directory.sync_check_schedule": {"queue": CeleryQueues.CPU},
    "directory.sync_run": {"queue": CeleryQueues.CPU},
    # GDPR Art. 17 reconciliation (issue #442): small DB reads plus, when there is
    # deferred work, object-storage and OpenSearch deletes. Utility — never gpu.
    "gdpr.erasure_reconcile": {"queue": CeleryQueues.UTILITY},
    "gdpr.erasure_execute_due": {"queue": CeleryQueues.UTILITY},
    "erasure.receipt_reverify": {"queue": CeleryQueues.UTILITY},
    # FedRAMP AC-2 account-inactivity expiration: small DB reads/writes only.
    # Utility — never gpu.
    "account.inactivity_sweep": {"queue": CeleryQueues.UTILITY},
    # FedRAMP AC-10 concurrent-session ceiling (issue #632): one grouped
    # query plus small per-user UPDATEs. Utility — never gpu.
    "session.cap_sweep": {"queue": CeleryQueues.UTILITY},
    # Expired refresh-token cleanup: one indexed range DELETE. Utility, never gpu.
    "auth.cleanup_expired_refresh_tokens": {"queue": CeleryQueues.UTILITY},
    # Chat retention: one settings read, then bounded deletes. Utility — never gpu.
    "chat.retention_sweep": {"queue": CeleryQueues.UTILITY},
    # Legal record / voiceprint retention: bounded selection + delete or dispatch. Utility.
    "legal.acceptance_retention_sweep": {"queue": CeleryQueues.UTILITY},
    "audit.retention_sweep": {"queue": CeleryQueues.UTILITY},
    # Governance audit outbox relay (v459): small DB claims plus one sink write per event.
    "audit.outbox_relay": {"queue": CeleryQueues.UTILITY},
    "privacy.voiceprint_retention_sweep": {"queue": CeleryQueues.UTILITY},
    # Finishes the centroid work of a purge a dead worker left (v461, issue #1334).
    "privacy.purge_reverify": {"queue": CeleryQueues.UTILITY},
    # Opt-in anonymous usage report (issue #1019): one settings read, at most one 5 s POST.
    "telemetry.usage_report": {"queue": CeleryQueues.UTILITY},
    # Benchmark-timing bookkeeping (one Redis hash -> one DB row), dispatched with a
    # countdown after postprocess. Utility, so telemetry never takes a cpu slot.
    "pipeline_timing.flush_tail": {"queue": CeleryQueues.UTILITY},
    # Admin-triggered storage recovery (Feature B, issue #969): ffprobe re-probes,
    # sidecar reads, and a paced yt-dlp metadata-only harvest. CPU — never gpu.
    "recovery.youtube_metadata_fetch": {"queue": CeleryQueues.CPU},
    "recovery.youtube_metadata_backfill": {"queue": CeleryQueues.CPU},
    "recovery.media_duration_backfill": {"queue": CeleryQueues.CPU},
}


#: Tasks deliberately absent from ``task_routes`` because every dispatch site picks the
#: queue at call time. Each needs a written reason. Read by ``unrouted_task_names()``,
#: which both the worker-startup warning and ``tests/unit/test_task_routes_complete.py``
#: use, so the runtime check and the test cannot drift apart (issue #1268).
INTENTIONALLY_UNROUTED_TASKS: dict[str, str] = {
    "transcription.gpu_transcribe": "dispatch.py routes it to 'gpu' or 'cloud-asr' per plan",
    "transcription.cpu_transcribe": "dispatch.py routes it to 'cpu-transcribe' at call time",
    # Dispatched from exactly one place — _dispatch_gpu_split_diarize_chain in
    # tasks/transcription/core.py — which always .set(queue=GPU_DIARIZE) explicitly.
    # Deliberately NOT given a static route: "gpu-diarize" is only consumed under the
    # gpu-split compose profile, so a static default would send an accidental bare
    # dispatch to a queue with no consumer on every NON-split deployment, which is
    # precisely issue #703's failure mode (a reserved worker doing no work while files
    # sit in `processing` forever). An unrouted accident lands on 'celery' and is at
    # least visible; a wrongly-routed one is silent.
    "transcription.diarize_gpu": "gpu-split only; core.py sets gpu-diarize, or gpu if unstaffed",
}


#: The queues each INTENTIONALLY_UNROUTED stage can be dispatched to. The orphan reaper reads
#: exactly these (plus the statically routed stages' queues) to find a run waiting on a queue
#: list. ``tests/unit/test_broker_orphans_queued_runs.py`` fails if a stage listed above is
#: missing here.
UNROUTED_STAGE_QUEUES: dict[str, tuple[str, ...]] = {
    "transcription.gpu_transcribe": (
        CeleryQueues.GPU,
        CeleryQueues.CLOUD_ASR,
        CeleryQueues.GPU_TRANSCRIBE,
    ),
    "transcription.cpu_transcribe": (CeleryQueues.CPU_TRANSCRIBE,),
    "transcription.diarize_gpu": (CeleryQueues.GPU_DIARIZE, CeleryQueues.GPU),
}
