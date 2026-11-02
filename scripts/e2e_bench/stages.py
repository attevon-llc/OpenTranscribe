"""Collect Redis timing/VRAM data for a task and derive stage durations and gaps."""

import json

import redis


def collect_benchmark_data(r: redis.Redis, task_id: str) -> dict:
    """Collect all benchmark timing data from Redis.

    The hash carries descriptive markers alongside the timestamps (``whisper_model`` for
    one), so anything non-numeric is kept as text instead of failing the whole run.
    """
    key = f'benchmark:{task_id}'
    raw = r.hgetall(key)
    data: dict = {}
    for raw_name, raw_value in raw.items():
        name, value = raw_name.decode(), raw_value.decode()
        try:
            data[name] = float(value)
        except ValueError:
            data[name] = value
    return data


def collect_vram_data(r: redis.Redis, task_id: str) -> dict | None:
    """Collect VRAM profiler data from Redis."""
    key = f'gpu:profile:{task_id}'
    raw = r.get(key)
    if raw:
        return json.loads(raw)
    return None


def calculate_stages(data: dict) -> dict:
    """Calculate stage durations and inter-stage gaps.

    Covers every marker added by the Phase 1 timing audit plan plus the five
    original markers (dispatch_timestamp, preprocess_end, gpu_received,
    gpu_end, postprocess_received). Missing markers are simply skipped so the
    legacy reprocess flow (which only populates the original five) still
    reports clean numbers.
    """
    stages: dict[str, float] = {}

    def _d(end_key: str, start_key: str) -> None:
        start = data.get(start_key)
        end = data.get(end_key)
        if start and end and end >= start:
            stages[f'{start_key}__to__{end_key}'] = end - start

    # ---------- Stage 0-8: client + API ingress ----------
    client_hash_s = data.get('client_hash_start')
    client_hash_e = data.get('client_hash_end')
    if client_hash_s and client_hash_e:
        stages['client_hash_duration'] = client_hash_e - client_hash_s

    http_start = data.get('http_request_received')
    http_read = data.get('http_read_complete')
    http_valid = data.get('http_validation_end')
    minio_s = data.get('minio_put_start')
    minio_e = data.get('minio_put_end')
    thumb_s = data.get('thumbnail_start')
    thumb_e = data.get('thumbnail_end')
    db_s = data.get('db_commit_start')
    db_e = data.get('db_commit_end')
    http_end = data.get('http_response_end')
    dispatch = data.get('dispatch_timestamp')

    if http_start and http_read:
        stages['http_read_duration'] = http_read - http_start
    if http_read and http_valid:
        stages['http_validation_duration'] = http_valid - http_read
    if minio_s and minio_e:
        stages['minio_put_duration'] = minio_e - minio_s
    if thumb_s and thumb_e:
        stages['thumbnail_duration'] = thumb_e - thumb_s
    if db_s and db_e:
        stages['db_commit_duration'] = db_e - db_s
    if http_start and http_end:
        stages['http_total_duration'] = http_end - http_start

    # ---------- Stage 9-10: queue wait + preprocess ----------
    pre_pre = data.get('preprocess_task_prerun')
    pre_end = data.get('preprocess_end')
    if dispatch and pre_pre:
        stages['dispatch_to_preprocess_prerun'] = pre_pre - dispatch
    if pre_pre and pre_end:
        stages['preprocess_body_duration'] = pre_end - pre_pre
    if dispatch and pre_end:
        stages['preprocess_duration'] = pre_end - dispatch

    for name in ('media_download', 'ffmpeg', 'metadata', 'temp_upload'):
        _d(f'{name}_end', f'{name}_start')

    # ---------- Stage 11-12: GPU ----------
    gpu_recv = data.get('gpu_received')
    gpu_prerun = data.get('gpu_task_prerun')
    gpu_end = data.get('gpu_end')

    if pre_end and gpu_recv:
        stages['preprocess_to_gpu_gap'] = gpu_recv - pre_end
    if gpu_prerun and gpu_recv:
        stages['gpu_prerun_skew'] = abs(gpu_prerun - gpu_recv)
    if gpu_recv and gpu_end:
        stages['gpu_duration'] = gpu_end - gpu_recv

    _d('gpu_audio_load_end', 'gpu_audio_load_start')

    # ---------- Stage 13-14: postprocess ----------
    post_recv = data.get('postprocess_received')
    post_end = data.get('postprocess_end')
    completion = data.get('completion_notified')

    if gpu_end and post_recv:
        stages['gpu_to_postprocess_gap'] = post_recv - gpu_end
    if post_recv and post_end:
        stages['postprocess_duration'] = post_end - post_recv

    # ---------- Stage 16: user-perceived done ----------
    perceived_start = http_start or dispatch
    if perceived_start and completion:
        stages['user_perceived_duration'] = completion - perceived_start

    # ---------- Stage 17-22: async enrichment ----------
    for name in (
        'search_index',
        'search_index_chunks',
        'speaker_upsert',
        'waveform',
        'clustering',
        'summary',
    ):
        _d(f'{name}_end', f'{name}_start')

    # ---------- Stage 21: fully indexed done ----------
    async_ends = [
        data.get(k)
        for k in (
            'search_index_chunks_end',
            'search_index_end',
            'speaker_upsert_end',
            'waveform_end',
            'clustering_end',
            'summary_end',
            'post_end',
        )
        if data.get(k) is not None
    ]
    if perceived_start and async_ends:
        stages['fully_indexed_duration'] = max(async_ends) - perceived_start

    # ---------- Legacy totals kept for back-compat with older CSVs ----------
    if dispatch and gpu_end:
        stages['total_dispatch_to_gpu_end'] = gpu_end - dispatch
    if dispatch and post_recv:
        stages['total_dispatch_to_postprocess'] = post_recv - dispatch

    return stages
