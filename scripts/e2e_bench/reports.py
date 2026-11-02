"""Console reports: per-run summary, detailed stage report and VRAM map."""

from e2e_bench.formatting import fmt_duration, fmt_size


def print_summary(all_results: list[dict]) -> None:
    """Print a compact summary table of benchmark results."""
    if not all_results:
        print('No results to summarize.')
        return

    stage_keys = set()
    for r in all_results:
        stage_keys.update(r.get('stages', {}).keys())
    stage_keys = sorted(stage_keys)

    print('\n' + '=' * 70)
    print('BENCHMARK SUMMARY')
    print('=' * 70)
    print(f'{"Stage":<35} {"Mean (s)":>10} {"Min (s)":>10} {"Max (s)":>10}')
    print('-' * 70)

    for key in stage_keys:
        values = [r['stages'][key] for r in all_results if key in r.get('stages', {})]
        if values:
            mean = sum(values) / len(values)
            print(f'{key:<35} {mean:>10.3f} {min(values):>10.3f} {max(values):>10.3f}')

    print('=' * 70)


def print_detailed_report(
    all_results: list[dict],
    all_vram: list[dict],
    file_meta: dict,
    file_uuid: str,
) -> None:
    """Print comprehensive detailed benchmark report."""
    if not all_results:
        print('No results to summarize.')
        return

    n = len(all_results)
    filename = file_meta.get('filename', 'unknown')
    audio_duration = file_meta.get('duration', 0)
    file_size = file_meta.get('file_size', 0)
    segment_count = file_meta.get('segment_count', 0)
    speaker_count = file_meta.get('speaker_count', 0)
    whisper_model = file_meta.get('whisper_model', 'unknown')

    # Calculate mean/min/max for each stage
    def _stats(key: str) -> tuple[float, float, float]:
        vals = [r['stages'][key] for r in all_results if key in r.get('stages', {})]
        if not vals:
            return 0, 0, 0
        return sum(vals) / len(vals), min(vals), max(vals)

    pre_mean, pre_min, pre_max = _stats('preprocess_duration')
    gap1_mean, _, _ = _stats('preprocess_to_gpu_gap')
    gpu_mean, gpu_min, gpu_max = _stats('gpu_duration')
    gap2_mean, _, _ = _stats('gpu_to_postprocess_gap')
    total_mean, total_min, total_max = _stats('total_dispatch_to_postprocess')

    # If total not available, estimate from components
    if total_mean == 0:
        total_mean = pre_mean + gap1_mean + gpu_mean + gap2_mean
        total_min = pre_min + gpu_min
        total_max = pre_max + gpu_max

    # Percentages
    def _pct(val: float) -> str:
        return f'{(val / total_mean * 100):.1f}%' if total_mean > 0 else 'N/A'

    # Realtime factor
    rt_factor = audio_duration / gpu_mean if gpu_mean > 0 else 0
    rt_total = audio_duration / total_mean if total_mean > 0 else 0

    # WAV size estimate (16kHz mono PCM_S16LE)
    wav_size_mb = (audio_duration * 16000 * 2) / (1024**2) if audio_duration > 0 else 0

    print('\n' + '=' * 76)
    print('SINGLE FILE BENCHMARK REPORT')
    print('=' * 76)
    print(f'File:            {filename}')
    print(f'UUID:            {file_uuid}')
    print(f'Audio Duration:  {fmt_duration(audio_duration)} ({audio_duration:.0f}s)')
    print(f'File Size:       {fmt_size(file_size)}')
    print(f'WAV Size (est):  {wav_size_mb:.0f} MB (16kHz mono PCM_S16LE)')
    print(f'Whisper Model:   {whisper_model}')
    print(f'Iterations:      {n}')

    print(f'\n{"PIPELINE STAGE BREAKDOWN":}')
    print('-' * 76)
    print(f'{"Stage":<40} {"Mean":>8} {"Min":>8} {"Max":>8} {"% Total":>8}')
    print('-' * 76)
    print(
        f'{"1. CPU Preprocess":<40} {pre_mean:>7.1f}s {pre_min:>7.1f}s {pre_max:>7.1f}s {_pct(pre_mean):>8}'
    )
    print(f'{"2. Queue: CPU -> GPU":<40} {gap1_mean:>7.1f}s {"":>8} {"":>8} {_pct(gap1_mean):>8}')
    print(
        f'{"3. GPU Transcribe + Diarize":<40} {gpu_mean:>7.1f}s {gpu_min:>7.1f}s {gpu_max:>7.1f}s {_pct(gpu_mean):>8}'
    )
    print(
        f'{"4. Queue: GPU -> Postprocess":<40} {gap2_mean:>7.1f}s {"":>8} {"":>8} {_pct(gap2_mean):>8}'
    )
    print('-' * 76)
    print(
        f'{"TOTAL (dispatch to postprocess)":<40} {total_mean:>7.1f}s {total_min:>7.1f}s {total_max:>7.1f}s {"100.0%":>8}'
    )

    # VRAM profile from the last successful run
    valid_vram = [v for v in all_vram if v is not None]
    if valid_vram:
        last = valid_vram[-1]
        steps = last.get('steps', [])

        if steps:
            print(f'\n{"VRAM PROFILE (last run)":}')
            print('-' * 76)
            print(
                f'{"Step":<30} {"Duration":>10} {"Before MB":>10} {"After MB":>10} {"Delta MB":>10}'
            )
            print('-' * 76)

            # Group steps: snapshots (duration=0) and timed steps
            timed = [s for s in steps if not s.get('name', '').startswith('snapshot:')]

            for step in timed:
                name = step.get('name', 'unknown')
                dur = step.get('duration_s', 0)
                before = step.get('device_used_before_mb', 0)
                after = step.get('device_used_after_mb', 0)
                delta = step.get('device_delta_mb', 0)
                sign = '+' if delta >= 0 else ''
                print(
                    f'{name:<30} {fmt_duration(dur):>10} {before:>9.0f} {after:>9.0f} {sign}{delta:>9.0f}'
                )

            # Summary metrics from report
            peak_device = last.get('peak_device_used_mb', 0)
            peak_pytorch = last.get('peak_pytorch_mb', 0)
            total_prof_dur = last.get('total_duration_s', 0)
            num_spk = last.get('num_speakers', 0)

            print(f'\n  Peak device VRAM:     {peak_device:.0f} MB')
            print(f'  Peak PyTorch alloc:   {peak_pytorch:.0f} MB')
            if total_prof_dur > 0:
                print(f'  Profiled duration:    {fmt_duration(total_prof_dur)}')
            if num_spk > 0:
                print(f'  Speakers detected:    {num_spk}')

            # Extract sub-stage timings from VRAM profile
            transcription_step = next((s for s in timed if s['name'] == 'transcription'), None)
            diarization_step = next((s for s in timed if s['name'] == 'diarization'), None)
            model_load_step = next(
                (s for s in timed if s['name'] == 'model_load_transcriber'), None
            )
            speaker_step = next((s for s in timed if s['name'] == 'speaker_assignment'), None)

            if transcription_step or diarization_step:
                print(f'\n{"GPU SUB-STAGE BREAKDOWN":}')
                print('-' * 76)
                print(f'{"Sub-stage":<30} {"Duration":>10} {"% of GPU":>10} {"Realtime":>10}')
                print('-' * 76)

                if model_load_step:
                    dur = model_load_step['duration_s']
                    pct = (dur / gpu_mean * 100) if gpu_mean > 0 else 0
                    print(
                        f'{"  Model load/warmup":<30} {fmt_duration(dur):>10} {pct:>9.1f}% {"":>10}'
                    )

                if transcription_step:
                    dur = transcription_step['duration_s']
                    pct = (dur / gpu_mean * 100) if gpu_mean > 0 else 0
                    rt = audio_duration / dur if dur > 0 else 0
                    print(
                        f'{"  Whisper transcription":<30} {fmt_duration(dur):>10} {pct:>9.1f}% {rt:>9.1f}x'
                    )

                if diarization_step:
                    dur = diarization_step['duration_s']
                    pct = (dur / gpu_mean * 100) if gpu_mean > 0 else 0
                    rt = audio_duration / dur if dur > 0 else 0
                    print(
                        f'{"  PyAnnote diarization":<30} {fmt_duration(dur):>10} {pct:>9.1f}% {rt:>9.1f}x'
                    )

                if speaker_step:
                    dur = speaker_step['duration_s']
                    pct = (dur / gpu_mean * 100) if gpu_mean > 0 else 0
                    print(
                        f'{"  Speaker assignment":<30} {fmt_duration(dur):>10} {pct:>9.1f}% {"":>10}'
                    )

                # Accounted vs unaccounted GPU time
                accounted = sum(
                    s['duration_s']
                    for s in timed
                    if s['name']
                    in (
                        'model_load_transcriber',
                        'transcription',
                        'diarization',
                        'speaker_assignment',
                    )
                )
                unaccounted = gpu_mean - accounted
                if unaccounted > 1:
                    pct = (unaccounted / gpu_mean * 100) if gpu_mean > 0 else 0
                    print(
                        f'{"  Other (DB save, cleanup)":<30} {fmt_duration(unaccounted):>10} {pct:>9.1f}% {"":>10}'
                    )

    else:
        print('\n  (No VRAM profile data — set ENABLE_VRAM_PROFILING=true in .env)')

    # Performance metrics
    print(f'\n{"PERFORMANCE METRICS":}')
    print('-' * 76)
    print(
        f'  Realtime Factor (GPU only):  {rt_factor:.1f}x '
        f'({fmt_duration(audio_duration)} audio in {fmt_duration(gpu_mean)})'
    )
    print(
        f'  Realtime Factor (total):     {rt_total:.1f}x '
        f'({fmt_duration(audio_duration)} audio in {fmt_duration(total_mean)})'
    )
    gpu_util = (gpu_mean / total_mean * 100) if total_mean > 0 else 0
    print(f'  GPU Utilization:             {gpu_util:.1f}% (GPU time / total wall clock)')
    print(f'  Segments:                    {segment_count}')
    print(f'  Speakers:                    {speaker_count}')

    # Projection for 1400 files
    if rt_factor > 0:
        print(f'\n{"QUICK PROJECTIONS (based on this file)":}')
        print('-' * 76)
        print(f'  If avg file = {fmt_duration(audio_duration)} at {rt_factor:.1f}x realtime:')
        for workers in [1, 2, 4, 5, 9]:
            # Sub-linear scaling: ~15% overhead per additional concurrent task
            eff = 1.0 if workers == 1 else workers * (1 / (1 + 0.15 * (workers - 1)))
            time_per_file = total_mean / eff
            total_1400 = 1400 * time_per_file
            print(
                f'    {workers} worker(s):  ~{fmt_duration(total_1400)} for 1400 files '
                f'({total_1400 / 3600:.1f} hours)'
            )

    print('=' * 76)


def print_vram_summary(all_vram: list[dict]) -> None:
    """Print VRAM profiler summary (compact mode)."""
    valid = [v for v in all_vram if v is not None]
    if not valid:
        print('\n  (No VRAM profile data — set ENABLE_VRAM_PROFILING=true in .env)')
        return

    print('\n' + '=' * 70)
    print('VRAM PROFILE (last run)')
    print('=' * 70)

    last = valid[-1]
    steps = last.get('steps', [])

    # VRAMProfiler.get_report() returns steps as a list of dicts
    if isinstance(steps, list) and steps:
        # Separate snapshots and timed steps
        timed = [s for s in steps if not s.get('name', '').startswith('snapshot:')]
        snapshots = [s for s in steps if s.get('name', '').startswith('snapshot:')]

        if snapshots:
            print(f'\n{"Snapshot":<35} {"Device Used (MB)":>17} {"Free (MB)":>12}')
            print('-' * 65)
            for snap in snapshots:
                name = snap.get('name', '').replace('snapshot:', '')
                used = snap.get('device_used_after_mb', 0)
                free = snap.get('device_free_mb', 0)
                print(f'{name:<35} {used:>17.0f} {free:>12.0f}')

        if timed:
            print(f'\n{"Step":<30} {"Duration (s)":>12} {"Peak VRAM (MB)":>15}')
            print('-' * 60)
            for step in timed:
                name = step.get('name', 'unknown')
                dur = step.get('duration_s', 0)
                peak = step.get('pt_peak_mb', 0)
                print(f'{name:<30} {dur:>12.3f} {peak:>15.0f}')

        # Derived metrics from snapshots
        snap_map = {s['name'].replace('snapshot:', ''): s for s in snapshots}
        ps = snap_map.get('pipeline_start', {})
        at = snap_map.get('after_transcriber_loaded', {})
        mw = snap_map.get('models_warm_no_inference', {})

        if ps and at:
            whisper_vram = at.get('device_used_after_mb', 0) - ps.get('device_used_after_mb', 0)
            print(f'\nWhisper warm VRAM:  {whisper_vram:.0f} MB')

        if at and mw:
            pyannote_vram = mw.get('device_used_after_mb', 0) - at.get('device_used_after_mb', 0)
            print(f'PyAnnote warm VRAM: {pyannote_vram:.0f} MB')

    elif isinstance(steps, dict) and steps:
        # Legacy dict format (shouldn't happen with current VRAMProfiler, but safe)
        print(f'\n{"Step":<30} {"Duration (s)":>12} {"Peak VRAM (MB)":>15}')
        print('-' * 60)
        for name, step in steps.items():
            dur = step.get('duration_s', 0)
            peak = step.get('peak_allocated_mb', step.get('pt_peak_mb', 0))
            print(f'{name:<30} {dur:>12.3f} {peak:>15.0f}')

    print('=' * 70)
