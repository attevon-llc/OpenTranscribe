"""Argument parsing and the top-level benchmark loop."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

import redis
from bench_common import load_redis_url

from bench_parallel.backend_api import (
    BENCH_UPLOAD_PREFIX,
    UPLOADED_UUIDS,
    db_query,
    delete_uploaded_files,
    get_benchmark_files,
    get_valid_token,
)
from bench_parallel.config import BACKEND_URL, GPU_DEVICE_ID
from bench_parallel.formatting import now_ts
from bench_parallel.models import BatchResult
from bench_parallel.report_files import write_reports
from bench_parallel.runner import run_batch


def main():
    parser = argparse.ArgumentParser(
        description='Progressive parallel transcription benchmark',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--batches',
        default='1,3,5,8,12,20',
        help='Comma-separated batch sizes to test (default: 1,3,5,8,12,20)',
    )
    parser.add_argument(
        '--output',
        default='benchmarks',
        help='Output directory for CSV results (default: benchmarks/)',
    )
    parser.add_argument(
        '--gpu-id',
        type=int,
        default=GPU_DEVICE_ID,
        help=f'Host GPU device ID to monitor VRAM (default: {GPU_DEVICE_ID})',
    )
    parser.add_argument(
        '--cooldown',
        type=int,
        default=30,
        help='Seconds to wait between batches for GPU to settle (default: 30)',
    )
    parser.add_argument(
        '--min-duration',
        type=int,
        default=60,
        help='Minimum file duration in seconds for selection (default: 60 = 1 minute)',
    )
    parser.add_argument(
        '--max-duration',
        type=int,
        default=0,
        help='Maximum file duration in seconds (default: 0 = no limit). '
        'Use with --min-duration to select files in a narrow range for fair comparison.',
    )
    parser.add_argument(
        '--file-uuids',
        default='',
        help='Comma-separated UUIDs to use instead of auto-selecting from DB. '
        'Ensures consistent file selection across test runs.',
    )
    parser.add_argument(
        '--sequential',
        action='store_true',
        help='Process each file individually in sequence (for duration curve testing). '
        'Ignores --batches and runs batch_size=1 for each file in the list.',
    )
    parser.add_argument(
        '--corpus-file',
        default='',
        help='Path to corpus JSON (e.g. docs/benchmark-corpus/corpus.json). '
        'Loads a fixed, ordered file list for repeatable whitepaper-quality runs. '
        'Overrides --file-uuids and auto-selection; --batches N takes the first N files '
        'from the active profile.',
    )
    parser.add_argument(
        '--profile',
        default='by_duration',
        help='Corpus profile to use when --corpus-file is set. '
        'by_duration (default): duration-ascending, good for VRAM ceiling tests — '
        'short files fail fast on OOM. '
        'mixed: round-robin across tiers so --batches 4 = one file per tier; '
        'best for real-world scheduler and throughput tests. '
        'mixed_hard: hardest file from each tier, always 4 files. '
        'Any custom profile name defined in the corpus JSON "profiles" section.',
    )
    parser.add_argument(
        '--shuffle',
        action='store_true',
        help='Randomly shuffle the file order after profile selection. '
        'Simulates a real-world queue where files arrive in unpredictable order — '
        'no size bias. Combine with --profile mixed for the most realistic stress test.',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without triggering reprocessing',
    )
    parser.add_argument(
        '--upload',
        action='store_true',
        help='Upload fresh files (real user flow: POST /api/files) instead of reprocessing '
        'existing UUIDs. Each file is read from --audio-dir/<filename> in the corpus. '
        'Requires a clean DB (no duplicate hashes) — the soak orchestrator clears bench data '
        'before each level. corpus.json UUIDs are ignored; fresh UUIDs come from the uploads.',
    )
    parser.add_argument(
        '--keep-uploads',
        action='store_true',
        help='With --upload: do NOT delete the uploaded files when the run ends (default: '
        'delete them, including after Ctrl-C or a failure).',
    )
    parser.add_argument(
        '--audio-dir',
        default='benchmark/test_audio',
        help='Directory holding the source audio files (matched by corpus filename) for '
        '--upload mode (default: benchmark/test_audio).',
    )
    parser.add_argument(
        '--stats-containers',
        default=os.environ.get('BENCHMARK_STATS_CONTAINERS', ''),
        help='Comma-separated container names to sample CPU%%/RAM from via docker stats '
        '(e.g. otbench-celery-worker,otbench-celery-cpu-worker). Empty = skip CPU/RAM sampling.',
    )
    parser.add_argument(
        '--run-label',
        default='',
        help='Label embedded in metrics.json identifying this run '
        '(e.g. phase1_a6000_solo_conc8). Used by the collator.',
    )
    args = parser.parse_args()

    batch_sizes = [int(x.strip()) for x in args.batches.split(',')]
    max_files = max(batch_sizes)
    output_dir = Path(args.output)
    stats_containers = [c.strip() for c in args.stats_containers.split(',') if c.strip()]
    corpus: dict = {}

    print('=' * 80)
    print('PARALLEL TRANSCRIPTION BENCHMARK')
    print('=' * 80)
    print(f'Batch sizes:    {batch_sizes}')
    print(f'GPU monitor:    GPU {args.gpu_id}')
    print(f'Output dir:     {output_dir}')
    print(f'Backend:        {BACKEND_URL}')

    # Get files — corpus file (preferred) > explicit UUIDs > auto-select from DB
    if args.corpus_file:
        corpus_path = Path(args.corpus_file)
        if not corpus_path.exists():
            print(f'ERROR: corpus file not found: {corpus_path}', file=sys.stderr)
            sys.exit(1)
        with open(corpus_path) as fh:
            corpus = json.load(fh)
        corpus_files = list(corpus['files'])

        # Apply profile ordering
        profiles = corpus.get('profiles', {})
        profile_name = args.profile
        if profile_name in profiles:
            profile = profiles[profile_name]
            indices = profile['indices']
            corpus_files = [corpus['files'][i] for i in indices]
            profile_desc = profile.get('description', '')
            print(f'  Profile: {profile_name} — {profile_desc[:80]}')
        elif profile_name != 'by_duration':
            print(
                f'  WARNING: profile "{profile_name}" not found in corpus, '
                'falling back to by_duration order',
                file=sys.stderr,
            )

        total_h = sum(f['duration_s'] for f in corpus_files) / 3600
        print(
            f'\nCorpus: {corpus_path.name}  —  {len(corpus_files)} files, {total_h:.2f}h total audio'
        )
        tier_counts: dict[int, int] = {}
        for f in corpus_files:
            tier_counts[f['tier']] = tier_counts.get(f['tier'], 0) + 1
        tier_info = corpus.get('tiers', {})
        for t, cnt in sorted(tier_counts.items()):
            label = tier_info.get(str(t), {}).get('label', f'tier {t}')
            rng = tier_info.get(str(t), {}).get('range', '')
            print(f'  Tier {t} ({label:10s} {rng}): {cnt} files')

        # Map corpus fields to the internal format in profile order
        files = [
            {
                'uuid': f['uuid'],
                'filename': f['filename'],
                'duration': float(f['duration_s']),
                'file_size': int(f.get('size_mb', 0)) * 1024 * 1024,
                'tier': f.get('tier'),
            }
            for f in corpus_files
        ]

        if args.shuffle:
            random.shuffle(files)
            print('  Order: shuffled (random)')
    elif args.file_uuids:
        uuids = [u.strip() for u in args.file_uuids.split(',') if u.strip()]
        print(f'\nUsing {len(uuids)} specified files...')
        uuid_list = "','".join(uuids)
        rows = db_query(
            f'SELECT uuid, filename, duration, file_size '
            f"FROM media_file WHERE uuid IN ('{uuid_list}') AND file_size > 0 "
            f'ORDER BY duration DESC'
        )
        files = []
        for row in rows:
            files.append(
                {
                    'uuid': row[0].strip(),
                    'filename': row[1].strip(),
                    'duration': float(row[2].strip()),
                    'file_size': int(row[3].strip()),
                }
            )
    else:
        dur_desc = f'>= {args.min_duration}s'
        if args.max_duration > 0:
            dur_desc += f', <= {args.max_duration}s'
        print(f'\nFetching {max_files} longest completed files ({dur_desc})...')
        files = get_benchmark_files(
            max_files,
            min_duration=args.min_duration,
            max_duration=args.max_duration,
        )
    if len(files) < max_files:
        print(f'WARNING: Only {len(files)} files available (need {max_files})')
        # Trim batch sizes to what we have
        batch_sizes = [b for b in batch_sizes if b <= len(files)]

    print(f'Found {len(files)} files:')
    for i, f in enumerate(files):
        hrs = f['duration'] / 3600
        print(f'  {i + 1:>2}. {f["filename"][:55]:<55} {hrs:.1f}h ({f["duration"]:.0f}s)')

    if args.dry_run:
        print('\n[DRY RUN] Would run these batches:')
        for bs in batch_sizes:
            print(f'  Batch {bs}: {", ".join(f["filename"][:30] for f in files[:bs])}')
        return

    # Authenticate
    print('\nAuthenticating...')
    token = get_valid_token()
    print('Authenticated.')

    # Connect to Redis
    r = redis.from_url(load_redis_url(), decode_responses=False)
    r.ping()
    print('Redis connected.')

    # Sequential mode: process each file individually (for duration curve testing)
    if args.sequential:
        batch_sizes = [1] * len(files)

    try:
        # Run batches
        all_batches: list[BatchResult] = []
        file_offset = 0
        for i, batch_size in enumerate(batch_sizes):
            if i > 0:
                print(f'\n  Cooling down {args.cooldown}s before next batch...')
                time.sleep(args.cooldown)

            # In sequential mode, advance through the file list one at a time
            if args.sequential:
                batch_files = files[file_offset : file_offset + 1]
                file_offset += 1
                if not batch_files:
                    break
            else:
                batch_files = files

            batch_result = run_batch(
                token,
                r,
                batch_files,
                batch_size,
                args.gpu_id,
                upload_mode=args.upload,
                audio_dir=args.audio_dir,
                stats_containers=stats_containers,
            )
            all_batches.append(batch_result)

        # Write reports
        corpus_total_h = sum(f['duration'] for f in files) / 3600 if args.corpus_file else None
        corpus_label = Path(args.corpus_file).name if args.corpus_file else None
        write_reports(
            all_batches,
            output_dir,
            corpus_total_audio_h=corpus_total_h,
            corpus_name=corpus_label,
            run_label=args.run_label,
        )
    finally:
        # Uploads exist whether or not the run finished: Ctrl-C and a failed batch leave
        # them just as a clean run does. --keep-uploads opts out.
        if args.upload and UPLOADED_UUIDS and not args.keep_uploads:
            print(f'\nDeleting {len(UPLOADED_UUIDS)} uploaded benchmark file(s)...')
            left = delete_uploaded_files(get_valid_token(), UPLOADED_UUIDS)
            if left:
                print(
                    f'  {left} upload(s) NOT deleted; scripts/cleanup-test-data.py sweeps '
                    f'the {BENCH_UPLOAD_PREFIX}* shape.',
                    file=sys.stderr,
                )

    print(f'\nBenchmark complete at {now_ts()}')
