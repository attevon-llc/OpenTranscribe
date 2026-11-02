"""Slow-call rules: which calls count as slow work inside an open DB session."""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------- slow-call rules
#
# Each rule is (category, exact call names, dotted-path substrings). A call fires the rule
# when its FINAL attribute is in the name set, or when its dotted path contains one of the
# substrings. Both are needed: `subprocess.run` is only recognisable by its path, while
# `client.download_file` is only recognisable by its method name.


@dataclass(frozen=True)
class Rule:
    category: str
    names: frozenset[str]
    paths: tuple[str, ...]


RULES: tuple[Rule, ...] = (
    Rule(
        'session-subprocess',
        frozenset(
            {
                'run_ffmpeg',
                '_run_ffmpeg',
                '_probe_audio_codec',
                'stitch_files',
                'extract_audio_from_video',
                'extract_audio_segment_np',
                'prepare_audio_for_transcription',
                'check_output',
                'check_call',
            }
        ),
        ('subprocess.run', 'subprocess.popen', 'subprocess.call', 'os.system', 'ffmpeg'),
    ),
    Rule(
        'session-object-storage',
        frozenset(
            {
                'upload_file',
                'upload_file_tuned',
                'upload_bytes',
                'download_file',
                'download_temp_audio',
                'presigned_get_object',
                'presigned_put_object',
                'get_presigned_url',
                'get_presigned_download_url',
                'fput_object',
                'fget_object',
                'put_object',
                'get_object',
                'remove_object',
                'delete_object',
                'copy_object',
                'delete_file_storage_artifacts',
            }
        ),
        ('minio', 'boto3', 's3_client.', 'storage_backend.'),
    ),
    Rule(
        'session-search-engine',
        frozenset(
            {
                # (``index_summary`` / ``delete_summary`` / ``get_max_version`` were
                # here for the retired ``transcript_summaries`` index, #67. Two of
                # those names no longer exist anywhere; ``delete_summary`` now names
                # a Postgres-only endpoint handler, so keeping it would make this
                # detector fire on a call that touches no search engine.)
                'reindex_transcript',
                'store_profile_embedding_v4',
                'bulk_add_speaker_embeddings_v4',
                'add_speaker_embedding',
                'get_speaker_document',
                'update_speaker_segment_count',
                'ensure_v4_index_exists',
                'delete_by_query',
                'msearch',
            }
        ),
        ('opensearch', 'os_client.'),
    ),
    Rule(
        'session-http',
        frozenset({'urlopen'}),
        (
            'requests.get',
            'requests.post',
            'requests.put',
            'requests.delete',
            'requests.request',
            'httpx.',
            'aiohttp.',
            'urllib.request',
        ),
    ),
    Rule(
        'session-llm',
        frozenset(
            {
                'generate_summary',
                'identify_speakers',
                'extract_topics',
                'chat_completion',
                'create_from_user_settings',
                'create_from_system_settings',
            }
        ),
        ('completions.create', '.chat.completions'),
    ),
    Rule(
        'session-model',
        frozenset(
            {
                'load_models',
                'load_model',
                'from_pretrained',
                'get_cached_embedding_service',
                'get_cached_attribute_service',
                '_run_inference',
                'extract_embeddings_for_segments',
                'extract_embedding_from_file',
                'transcribe_audio',
                'diarize_audio',
            }
        ),
        ('whisperx.', 'pyannote.', 'torch.load'),
    ),
    Rule(
        'session-smtp',
        frozenset({'send_email', 'sendmail', 'send_message'}),
        ('smtplib.',),
    ),
    Rule(
        'session-thread-pool',
        frozenset(
            {
                'ThreadPoolExecutor',
                'ProcessPoolExecutor',
                'as_completed',
                # #403: `services/chat/legs.py`'s process-wide leg
                # executor. `run_legs` is the one call site every fan-out
                # goes through, and it BLOCKS the caller until every leg's
                # future resolves — the same shape a bare
                # `with ThreadPoolExecutor() as pool: ...` already trips this
                # rule for, except the pool here lives outside the function
                # entirely, so nothing about the CALL SITE looks like a
                # thread pool unless the name itself is listed. Each leg can
                # itself make an OpenSearch/LLM/Postgres call, so calling
                # `run_legs` (or reaching straight for `get_executor`) inside
                # an open session holds that transaction for as long as the
                # SLOWEST leg, multiplied by nothing — but still exactly the
                # class of hold this whole file exists to catch.
                'run_legs',
                'get_executor',
            }
        ),
        ('concurrent.futures', 'legs.run_legs', 'legs.get_executor'),
    ),
)

#: Callables that OPEN a database session as a context manager.
#: ``session_factory`` and ``_short_session`` were added after the chat turn was phased
#: (e486f948): ``answer_aggregation`` takes a FACTORY rather than a ``Session``, precisely so
#: each statement group gets its own short transaction. Without both names the detector saw
#: those blocks as ordinary ``with`` statements and could not fire inside them at all —
#: reporting 0 findings, which is indistinguishable from a clean subsystem.
#:
#: BOTH are needed, and ``_short_session`` is the load-bearing one. ``session_factory``
#: only covers the direct ``with session_factory() as db:`` inside the helper, whose body is
#: a bare ``yield`` — nothing slow can live there. The CALLER's work runs during that yield,
#: in its own ``with _short_session(...)`` block, and that is the block a slow call would be
