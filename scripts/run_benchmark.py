#!/usr/bin/env python3
"""End-to-end engine-optimization benchmark orchestrator.

One clean, resumable control flow per phase-level:

    teardown -> configure env -> up fresh bench stack -> wait ready ->
    upload corpus like a user -> process + collect metrics -> teardown

The bench stack is fully isolated under the ``otbench`` compose project with
``otbench-*`` container names and throwaway ``*_bench_data`` volumes, so the dev
deployment on nvm/NAS is never touched. Because every level starts from an empty
DB (``down -v``), there is no orphan-resetting, no mid-run DB mutation, and no
force-recreate — the failure modes of the old soak script are gone by design.

Usage (via the wrapper):
    ./opentr.sh bench all   [--smoke|--quick|--full] [--phases a,b,c]
    ./opentr.sh bench phase <name> [--smoke|--quick|--full] [--conc N]

Resume: re-run the same command. Any level whose metrics.json already exists is
skipped, so an interrupted multi-hour run resumes where it stopped.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROJECT = 'otbench'
CORPUS = REPO / 'docs' / 'benchmark-corpus' / 'corpus.json'
AUDIO_DIR = REPO / 'benchmark' / 'test_audio'
# OUTROOT is overridable (BENCHMARK_OUTROOT) so A/B runs can write to separate dirs.
OUTROOT = Path(os.environ.get('BENCHMARK_OUTROOT', str(REPO / 'docs' / 'engine-benchmark-results')))
RESULTS_TSV = OUTROOT / 'results.tsv'
PY = REPO / 'backend' / 'venv' / 'bin' / 'python'
BACKEND_URL = os.environ.get('BENCHMARK_BACKEND_URL', 'http://localhost:5174')

# Compose file sets (always prefixed with -p otbench).
BASE_FILES = ['docker-compose.yml', 'docker-compose.gpu.yml', 'docker-compose.bench.yml']
GPU_SCALE_FILES = BASE_FILES + ['docker-compose.gpu-scale.yml', 'docker-compose.bench-gpu.yml']
GPU_SPLIT_FILES = BASE_FILES + ['docker-compose.bench-gpu.yml']
ALL_FILES = BASE_FILES + ['docker-compose.gpu-scale.yml', 'docker-compose.bench-gpu.yml']

# VRAM ceilings (MB) used to pick the best stable concurrency per card.
A6000_VRAM_CEILING = 47000
TI_VRAM_CEILING = 11000

# Readiness / disk thresholds.
HEALTH_TIMEOUT = 240
PING_TIMEOUT = 300
PRELOAD_TIMEOUT = 240
MIN_FREE_GB = 30


def log(msg: str) -> None:
    print(f'[{datetime.now():%H:%M:%S}] {msg}', flush=True)


# ---------------------------------------------------------------------------
# Phase definitions
# ---------------------------------------------------------------------------
@dataclass
class Phase:
    name: str
    mode: str  # solo | gpu-scale | gpu-split
    profile: str
    gpu_device: int = 0  # host GPU to monitor + run on (solo)
    sweep: list[int] = field(default_factory=list)  # solo concurrency sweep
    shuffle: bool = False
    sequential: bool = False
    # gpu-scale device overrides (None -> defaults: scaled=GPU0, default-worker=GPU1).
    scale_device_id: int | None = None
    default_device_id: int | None = None
    # homogeneous=True -> both cards run at the A6000 best concurrency (dual-A6000),
    # else the default worker uses the Ti best (A6000+Ti mix).
    homogeneous: bool = False
    # default_run=False -> only runs when explicitly named in --phases (e.g. uses the
    # LLM GPU, so it must never run in a plain --full sweep).
    default_run: bool = True


PHASES: list[Phase] = [
    Phase(
        'a6000_solo', 'solo', 'mixed', gpu_device=0, sweep=[1, 4, 8, 10, 12, 16, 20], shuffle=True
    ),
    Phase('ti_solo', 'solo', 'by_duration', gpu_device=1, sweep=[1, 2, 3, 4]),
    Phase('dual_gpu_scale', 'gpu-scale', 'mixed', gpu_device=0, shuffle=True),
    Phase('gpu_split', 'gpu-split', 'mixed', gpu_device=0, shuffle=True),
    Phase('duration_curve', 'solo', 'by_duration', gpu_device=0, sequential=True),
    # Dual-A6000 (GPU 0 + GPU 2): both big cards at the A6000 peak concurrency.
    # GPU 2 is normally the LLM card, so this is opt-in only (--phases dual_a6000).
    Phase(
        'dual_a6000',
        'gpu-scale',
        'mixed',
        gpu_device=0,
        shuffle=True,
        scale_device_id=2,
        default_device_id=0,
        homogeneous=True,
        default_run=False,
    ),
]


# ---------------------------------------------------------------------------
# Shell / compose helpers
# ---------------------------------------------------------------------------
def run(
    cmd: list[str],
    env: dict | None = None,
    check: bool = True,
    capture: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        cmd,
        cwd=REPO,
        env=full_env,
        check=check,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def compose(
    action: list[str],
    files: list[str],
    env: dict | None = None,
    profile: str = '',
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    cmd = ['docker', 'compose', '-p', PROJECT]
    for f in files:
        cmd += ['-f', f]
    if profile:
        cmd += ['--profile', profile]
    cmd += action
    cenv = {'COMPOSE_PROJECT_NAME': PROJECT}
    if profile:
        cenv['COMPOSE_PROFILES'] = profile
    cenv.update(env or {})
    return run(cmd, env=cenv, check=check, capture=capture)


def docker_rm_force(name_glob: str) -> None:
    res = run(['docker', 'ps', '-aq', '--filter', f'name={name_glob}'], capture=True, check=False)
    ids = [i for i in res.stdout.split() if i]
    if ids:
        run(['docker', 'rm', '-f', *ids], check=False, capture=True)


def teardown(env: dict) -> None:
    """Stop + wipe the bench stack. Safe: scoped to project otbench, *_bench_data only."""
    compose(['down', '-v', '--remove-orphans'], ALL_FILES, env=env, profile='', check=False)
    # Stragglers (scaled/split workers can linger if a profile run was interrupted).
    docker_rm_force(f'{PROJECT}-celery-worker-gpu')


def cleanup_legacy() -> None:
    """One-time: remove pre-rename leftovers that the new project name can't see."""
    log('clearing legacy bench leftovers (opentranscribe-* bench stack + gpu-scaled orphan)')
    run(
        [
            'docker',
            'compose',
            '-p',
            'transcribe-app',
            '-f',
            'docker-compose.yml',
            '-f',
            'docker-compose.gpu.yml',
            '-f',
            'docker-compose.bench.yml',
            'down',
            '--remove-orphans',
        ],
        check=False,
        capture=True,
    )
    docker_rm_force('transcribe-app-celery-worker-gpu')
    # Remove ONLY the old pre-rename bench volumes (exact *_bench_data names — never
    # the dev data volumes, which are named differently / are NAS bind mounts).
    for v in ('postgres', 'minio', 'redis', 'opensearch', 'flower'):
        run(['docker', 'volume', 'rm', f'transcribe-app_{v}_bench_data'], check=False, capture=True)


# ---------------------------------------------------------------------------
# Corpus subset building
# ---------------------------------------------------------------------------
def _seed_hash(filename: str) -> str:
    """The unique 8-char id token from a seed filename (seed_t{T}_{D}s_{HASH}_...)."""
    parts = filename.split('_')
    return parts[3] if len(parts) >= 4 and parts[0] == 'seed' else ''


def reconcile_to_disk(files: list[dict]) -> list[dict]:
    """Rewrite each corpus file's name to the REAL on-disk filename (matched by hash).

    corpus.json stored sanitized names (collapsed/dropped underscores) that differ
    from the actual yt-dlp filenames on disk, but the 8-char hash token is stable
    and unique. Files with no on-disk match (or only a partial .part download) are
    dropped — logged, never silently — so --upload only ever references real files.
    """
    disk_names: set[str] = set()
    disk_by_hash: dict[str, str] = {}
    for p in AUDIO_DIR.iterdir():
        if p.is_file() and p.suffix != '.part':
            disk_names.add(p.name)
            h = _seed_hash(p.name)
            if h:
                disk_by_hash[h] = p.name
    out, dropped = [], []
    for f in files:
        # Exact match first (synthetic baseline WAVs), then stable hash match (seed files).
        if f['filename'] in disk_names:
            out.append(dict(f))
            continue
        real = disk_by_hash.get(_seed_hash(f['filename']))
        if real:
            out.append({**f, 'filename': real})
        else:
            dropped.append(f['filename'])
    if dropped:
        log(
            f'corpus: {len(dropped)} file(s) have no on-disk match and were dropped: '
            + ', '.join(d[:40] for d in dropped[:5])
            + ('...' if len(dropped) > 5 else '')
        )
    return out


def build_subset(tier: str, dest: Path) -> tuple[Path, int, float]:
    """Write a corpus JSON subset for the requested tier; return (path, n_files, hours)."""
    corpus = json.loads(CORPUS.read_text())
    files = reconcile_to_disk(corpus['files'])
    by_dur = sorted(range(len(files)), key=lambda i: files[i]['duration_s'])

    if tier == 'full':
        chosen = list(range(len(files)))
    elif tier == 'smoke':
        # 3 shortest tier-1 clips — exercises the whole machine in minutes.
        t1 = [i for i in by_dur if files[i].get('tier') == 1]
        chosen = t1[:3] if len(t1) >= 3 else by_dur[:3]
    else:  # quick: a few shortest per tier (~10-15h spanning all tiers)
        per_tier = {1: 4, 2: 3, 3: 2, 4: 1}
        chosen = []
        for t, n in per_tier.items():
            chosen += [i for i in by_dur if files[i].get('tier') == t][:n]

    chosen_files = [files[i] for i in chosen]
    # Recompute profile indices over the subset (positions in chosen_files).
    sub_by_dur = sorted(range(len(chosen_files)), key=lambda i: chosen_files[i]['duration_s'])
    tiers: dict[int, list[int]] = {}
    for pos, fobj in enumerate(chosen_files):
        tiers.setdefault(fobj.get('tier', 0), []).append(pos)
    mixed: list[int] = []
    order = sorted(tiers)
    while any(tiers[t] for t in order):
        for t in order:
            if tiers[t]:
                mixed.append(tiers[t].pop(0))

    subset = {
        'version': corpus.get('version'),
        'tiers': corpus.get('tiers', {}),
        'files': chosen_files,
        'profiles': {
            'by_duration': {'description': 'subset duration-ascending', 'indices': sub_by_dur},
            'mixed': {'description': 'subset tier round-robin', 'indices': mixed},
        },
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(subset, indent=2))
    hours = sum(f['duration_s'] for f in chosen_files) / 3600
    return dest, len(chosen_files), hours


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
def preflight(skip_build: bool) -> None:
    log('===== PRE-FLIGHT =====')
    if not CORPUS.exists():
        sys.exit(f'FATAL: corpus not found: {CORPUS}')
    if not AUDIO_DIR.exists():
        sys.exit(f'FATAL: audio dir not found: {AUDIO_DIR}')

    # GPUs idle (never kill — just refuse if busy).
    res = run(
        [
            'nvidia-smi',
            '--query-gpu=index,memory.used,utilization.gpu',
            '--format=csv,noheader,nounits',
        ],
        capture=True,
        check=False,
    )
    if res.returncode != 0:
        sys.exit('FATAL: nvidia-smi failed — check GPU state before benchmarking.')
    log('GPU state:\n' + res.stdout.strip())
    for line in res.stdout.strip().splitlines():
        idx, used, _util = (p.strip() for p in line.split(','))
        if idx in ('0', '1') and int(used) > 2000:
            log(
                f'WARNING: GPU {idx} already using {used} MB — results may be skewed. '
                'Ensure no competing OpenTranscribe load is running.'
            )

    # Disk headroom on the docker data-root.
    root = '/var/lib/docker' if Path('/var/lib/docker').exists() else '/'
    free_gb = shutil.disk_usage(root).free / 1e9
    log(f'free disk on {root}: {free_gb:.0f} GB')
    if free_gb < MIN_FREE_GB:
        sys.exit(f'FATAL: only {free_gb:.0f} GB free on {root}; need >= {MIN_FREE_GB} GB.')

    if not skip_build:
        log('building opentranscribe-backend:bench from current branch (cached layers reused)...')
        run(
            [
                'docker',
                'build',
                '-t',
                'opentranscribe-backend:bench',
                '-f',
                'backend/Dockerfile.prod',
                'backend/',
            ]
        )
        log('backend:bench build complete.')
    log('===== PRE-FLIGHT OK =====')


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
def _http_ok(url: str) -> bool:
    res = run(['curl', '-sf', '--max-time', '4', url], check=False, capture=True)
    return res.returncode == 0


def wait_ready(gpu_workers: list[str]) -> None:
    """Wait for backend health + each GPU worker's ping + model-preload log marker."""
    log('waiting for backend health...')
    deadline = time.time() + HEALTH_TIMEOUT
    while time.time() < deadline:
        if _http_ok(f'{BACKEND_URL}/health'):
            log('backend healthy.')
            break
        time.sleep(5)
    else:
        sys.exit('FATAL: backend never became healthy.')

    for worker in gpu_workers:
        log(f'waiting for {worker} celery ping...')
        deadline = time.time() + PING_TIMEOUT
        while time.time() < deadline:
            res = run(
                ['docker', 'exec', worker, 'celery', '-A', 'app.core.celery', 'inspect', 'ping'],
                check=False,
                capture=True,
            )
            if 'pong' in (res.stdout + res.stderr):
                log(f'{worker} responds to ping.')
                break
            time.sleep(5)
        else:
            sys.exit(f'FATAL: {worker} never responded to celery ping.')

        # Models-preloaded marker — efficient, replaces blind sleeps. Best-effort.
        log(f'waiting for {worker} model preload...')
        deadline = time.time() + PRELOAD_TIMEOUT
        seen = False
        while time.time() < deadline:
            logs = run(['docker', 'logs', '--tail', '400', worker], check=False, capture=True)
            if 'GPU models preloaded and pinned' in (logs.stdout + logs.stderr):
                log(f'{worker} models preloaded.')
                seen = True
                break
            time.sleep(5)
        if not seen:
            log(
                f'WARNING: preload marker not seen for {worker} within {PRELOAD_TIMEOUT}s — '
                'proceeding after short grace.'
            )
            time.sleep(20)


def wait_auth(timeout: int = 240) -> None:
    """Wait until /api/auth/token returns a token.

    /health can go green before the backend finishes the fresh-DB migration chain
    and seeds the admin user, so the first auth POST may hang. Polling for a real
    token is the true readiness gate for the benchmark (it needs auth + a user).
    """
    email = os.environ.get('BENCHMARK_EMAIL', 'admin@example.com')
    password = os.environ.get('BENCHMARK_PASSWORD', 'password')
    log('waiting for auth (admin seed + migrations)...')
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = run(
            [
                'curl',
                '-s',
                '--max-time',
                '10',
                '-X',
                'POST',
                f'{BACKEND_URL}/api/auth/token',
                '-d',
                f'username={email}',
                '-d',
                f'password={password}',
            ],
            check=False,
            capture=True,
        )
        if res.returncode == 0 and 'access_token' in res.stdout:
            log('auth ready (token obtained).')
            return
        time.sleep(5)
    sys.exit(
        f'FATAL: auth endpoint never returned a token within {timeout}s '
        '(migrations/admin seed incomplete?).'
    )


# ---------------------------------------------------------------------------
# All-GPU sampler (captures both cards for the whole level)
# ---------------------------------------------------------------------------
class AllGpuSampler:
    def __init__(self, out_csv: Path, interval: float = 2.0):
        self.out_csv = out_csv
        self.interval = interval
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._t:
            self._t.join(timeout=5)

    def _run(self) -> None:
        with open(self.out_csv, 'w') as f:
            f.write('elapsed_s,gpu,mem_used_mb,util_pct,temp_c,power_w\n')
            t0 = time.time()
            while not self._stop.is_set():
                res = run(
                    [
                        'nvidia-smi',
                        '--query-gpu=index,memory.used,utilization.gpu,temperature.gpu,power.draw',
                        '--format=csv,noheader,nounits',
                    ],
                    check=False,
                    capture=True,
                )
                if res.returncode == 0:
                    el = time.time() - t0
                    for line in res.stdout.strip().splitlines():
                        parts = [p.strip() for p in line.split(',')]
                        if len(parts) >= 5:
                            f.write(f'{el:.1f},{",".join(parts[:5])}\n')
                    f.flush()
                self._stop.wait(self.interval)


# ---------------------------------------------------------------------------
# Level execution
# ---------------------------------------------------------------------------
def phase_env(phase: Phase, conc: int, dual_a6000: int, dual_ti: int) -> dict:
    """Env vars that configure the worker(s) for this level (compose interpolates them)."""
    env = {
        'WHISPER_MODEL': 'large-v3-turbo',
        'ENABLE_BENCHMARK_TIMING': 'true',
        'ENABLE_VRAM_PROFILING': 'true',
        'GPU_SCALE_ENABLED': 'false',
        'GPU_SCALE_DEFAULT_WORKER': '0',
        'ENGINE_GPU_SPLIT': 'false',
    }
    if phase.mode == 'solo':
        env['GPU_DEVICE_ID'] = str(phase.gpu_device)
        env['GPU_CONCURRENT_REQUESTS'] = str(conc)
    elif phase.mode == 'gpu-scale':
        # Dual-GPU: a scaled worker on one card + the default worker on the other.
        # Defaults: scaled=GPU0 (A6000), default-worker=GPU1 (Ti). dual_a6000 overrides
        # to scaled=GPU2, default-worker=GPU0 (both A6000). dual_a6000 = scaled-worker
        # concurrency, dual_ti = default-worker concurrency.
        env['GPU_SCALE_ENABLED'] = 'true'
        env['GPU_SCALE_DEFAULT_WORKER'] = '1'
        env['GPU_SCALE_DEVICE_ID'] = str(
            phase.scale_device_id if phase.scale_device_id is not None else 0
        )
        env['GPU_SCALE_WORKERS'] = str(dual_a6000)
        env['GPU_DEVICE_ID'] = str(
            phase.default_device_id if phase.default_device_id is not None else 1
        )
        env['GPU_CONCURRENT_REQUESTS'] = str(dual_ti)
    elif phase.mode == 'gpu-split':
        env['ENGINE_GPU_SPLIT'] = 'true'
        env['GPU_TRANSCRIBE_DEVICE_ID'] = '0'
        env['GPU_DIARIZE_DEVICE_ID'] = '1'
        env['GPU_CONCURRENT_REQUESTS'] = str(conc)
    return env


def files_and_profile(mode: str) -> tuple[list[str], str]:
    if mode == 'gpu-scale':
        return GPU_SCALE_FILES, 'gpu-scale'
    if mode == 'gpu-split':
        return GPU_SPLIT_FILES, 'gpu-split'
    return BASE_FILES, ''


def gpu_workers_for(mode: str) -> list[str]:
    if mode == 'gpu-scale':
        return [f'{PROJECT}-celery-worker-gpu-scaled', f'{PROJECT}-celery-worker']
    if mode == 'gpu-split':
        return [f'{PROJECT}-celery-worker-gpu-transcribe', f'{PROJECT}-celery-worker-gpu-diarize']
    return [f'{PROJECT}-celery-worker']


def stats_containers_for(mode: str) -> list[str]:
    base = [f'{PROJECT}-celery-cpu-worker']
    return gpu_workers_for(mode) + base


def oom_detected(gpu_workers: list[str]) -> bool:
    for w in gpu_workers:
        logs = run(['docker', 'logs', '--tail', '500', w], check=False, capture=True)
        blob = (logs.stdout + logs.stderr).lower()
        if 'out of memory' in blob or 'cuda error' in blob:
            return True
    return False


def run_level(
    phase: Phase, conc: int, corpus_path: Path, n_files: int, dual_a6000: int, dual_ti: int
) -> dict | None:
    label = phase.name if (phase.sequential or phase.mode != 'solo') else f'{phase.name}_conc{conc}'
    outdir = OUTROOT / label
    if (outdir / 'metrics.json').exists():
        log(f'skip {label} (metrics.json exists)')
        data: dict = json.loads((outdir / 'metrics.json').read_text())
        batches = data.get('batches', [])
        last = batches[-1] if batches else {}
        return {'label': label, 'stable': 'yes', **last}

    files, profile = files_and_profile(phase.mode)
    env = phase_env(phase, conc, dual_a6000, dual_ti)
    workers = gpu_workers_for(phase.mode)

    log(f'===== LEVEL {label} (mode={phase.mode} conc={conc}) =====')
    teardown(env)
    log('starting fresh bench stack...')
    compose(['up', '-d'], files, env=env, profile=profile)
    wait_ready(workers)
    wait_auth()

    outdir.mkdir(parents=True, exist_ok=True)
    sampler = AllGpuSampler(outdir / 'gpu_all.csv')
    sampler.start()

    cmd = [
        str(PY),
        'scripts/benchmark_parallel.py',
        '--corpus-file',
        str(corpus_path),
        '--profile',
        phase.profile,
        '--upload',
        '--audio-dir',
        str(AUDIO_DIR),
        '--gpu-id',
        str(phase.gpu_device),
        '--cooldown',
        '0',
        '--output',
        str(outdir),
        '--run-label',
        label,
    ]
    if phase.sequential:
        cmd += ['--sequential', '--batches', '1']
    else:
        cmd += ['--batches', str(n_files)]
    if phase.shuffle:
        cmd += ['--shuffle']

    bench_env = {
        'BENCHMARK_DB_CONTAINER': f'{PROJECT}-postgres',
        'BENCHMARK_BACKEND_URL': BACKEND_URL,
        'BENCHMARK_STATS_CONTAINERS': ','.join(stats_containers_for(phase.mode)),
        'BENCHMARK_EMAIL': os.environ.get('BENCHMARK_EMAIL', 'admin@example.com'),
        'BENCHMARK_PASSWORD': os.environ.get('BENCHMARK_PASSWORD', 'password'),
    }
    rc = run(cmd, env=bench_env, check=False).returncode
    sampler.stop()

    stable = 'oom' if oom_detected(workers) else 'yes'
    if rc != 0:
        log(f'ABORT: benchmark_parallel exited {rc} for {label} — not recording.')
        teardown(env)
        return None

    metrics = json.loads((outdir / 'metrics.json').read_text())
    batches = metrics.get('batches', [])
    last = batches[-1] if batches else {}
    record_result(phase.name, label, conc, phase.gpu_device, last, stable)
    log(
        f'result {label}: rtf={last.get("throughput_audio_hrs_per_wall_hr")} '
        f'vram_peak={last.get("vram_peak_mb")}MB cpu_avg={last.get("cpu_pct_avg")}% stable={stable}'
    )

    teardown(env)
    return {'label': label, 'stable': stable, **last}


# ---------------------------------------------------------------------------
# Results bookkeeping
# ---------------------------------------------------------------------------
def record_result(phase: str, label: str, conc: int, gpu: int, batch: dict, stable: str) -> None:
    OUTROOT.mkdir(parents=True, exist_ok=True)
    new = not RESULTS_TSV.exists()
    with open(RESULTS_TSV, 'a') as f:
        if new:
            f.write(
                'phase\tlabel\tconc\tgpu\trtf\tvram_peak_mb\tcpu_pct_avg\tram_mb_avg\t'
                'gpu_util_pct_avg\tstable\n'
            )
        f.write(
            f'{phase}\t{label}\t{conc}\t{gpu}\t'
            f'{batch.get("throughput_audio_hrs_per_wall_hr", "")}\t'
            f'{batch.get("vram_peak_mb", "")}\t{batch.get("cpu_pct_avg", "")}\t'
            f'{batch.get("ram_mb_avg", "")}\t{batch.get("gpu_util_pct_avg", "")}\t{stable}\n'
        )


def clear_results(selected: list[Phase]) -> None:
    """Wipe prior per-level run dirs + aggregate files so old data never confuses output.

    Only touches OUTROOT (regenerable benchmark output). Removes every level
    directory (anything containing a metrics.json or matching a known/selected
    phase prefix) plus the master/summary/corpus artifacts.
    """
    if not OUTROOT.exists():
        return
    log(f'--fresh: clearing prior benchmark data under {OUTROOT}')
    known = {p.name for p in PHASES} | {p.name for p in selected}
    removed = 0
    for child in OUTROOT.iterdir():
        if child.is_dir():
            is_level = (
                (child / 'metrics.json').exists()
                or any(child.name == k or child.name.startswith(f'{k}_conc') for k in known)
                or child.name.startswith('phase')
            )  # legacy failed-run dirs
            if is_level:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
    for f in [
        'results.tsv',
        'master_results.csv',
        'summary.md',
        'corpus_smoke.json',
        'corpus_quick.json',
        'corpus_full.json',
    ]:
        (OUTROOT / f).unlink(missing_ok=True)
    log(f'--fresh: removed {removed} level dirs + aggregate files')


def best_conc(phase: str, ceiling: int, fallback: int) -> int:
    """Highest-throughput stable concurrency under the VRAM ceiling, from results.tsv."""
    if not RESULTS_TSV.exists():
        return fallback
    best, best_rtf = fallback, -1.0
    for line in RESULTS_TSV.read_text().splitlines()[1:]:
        c = line.split('\t')
        if len(c) < 10 or c[0] != phase or c[9] != 'yes':
            continue
        try:
            conc, rtf, vram = int(c[2]), float(c[4]), float(c[5])
        except ValueError:
            continue
        if vram <= ceiling and rtf > best_rtf:
            best, best_rtf = conc, rtf
    return best


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description='End-to-end engine benchmark orchestrator')
    tier = ap.add_mutually_exclusive_group()
    tier.add_argument(
        '--smoke', action='store_true', help='all phases at conc 1 & 2 on shortest clips'
    )
    tier.add_argument('--quick', action='store_true', help='~10-15h subset (default)')
    tier.add_argument('--full', action='store_true', help='full ~58h corpus (paper run)')
    ap.add_argument('--phases', default='', help='comma list to limit which phases run')
    ap.add_argument('--conc', type=int, default=0, help='single concurrency override (solo phases)')
    ap.add_argument('--skip-build', action='store_true', help='skip rebuilding the bench image')
    ap.add_argument('--no-cleanup-legacy', action='store_true', help='skip one-time legacy cleanup')
    ap.add_argument(
        '--fresh',
        action='store_true',
        help='wipe prior per-level results + results.tsv before starting '
        '(disables resume — use for a clean run)',
    )
    args = ap.parse_args()

    tier_name = 'smoke' if args.smoke else 'full' if args.full else 'quick'
    only = {p.strip() for p in args.phases.split(',') if p.strip()}
    # Explicitly-named phases always run; an unfiltered run includes only default_run
    # phases (excludes e.g. dual_a6000, which uses the LLM GPU).
    if only:
        selected = [p for p in PHASES if p.name in only]
    else:
        selected = [p for p in PHASES if p.default_run]
    if not selected:
        sys.exit(f'No phases matched {only}. Known: {[p.name for p in PHASES]}')

    log(f'Benchmark tier={tier_name}  phases={[p.name for p in selected]}')
    if args.fresh:
        clear_results(selected)
    preflight(args.skip_build)
    if not args.no_cleanup_legacy:
        cleanup_legacy()

    corpus_path, n_files, hours = build_subset(tier_name, OUTROOT / f'corpus_{tier_name}.json')
    log(f'corpus subset ({tier_name}): {n_files} files, {hours:.1f}h audio')

    for phase in selected:
        if phase.mode == 'solo' and not phase.sequential:
            sweep = [args.conc] if args.conc else ([1, 2] if tier_name == 'smoke' else phase.sweep)
            sweep = [c for c in sweep if c <= n_files]
            for conc in sweep:
                r = run_level(phase, conc, corpus_path, n_files, 0, 0)
                if r and r.get('stable') == 'oom':
                    log(f'{phase.name}: OOM at conc={conc} — stopping sweep.')
                    break
        elif phase.mode in ('gpu-scale', 'gpu-split'):
            a6 = 2 if tier_name == 'smoke' else best_conc('a6000_solo', A6000_VRAM_CEILING, 8)
            ti = 2 if tier_name == 'smoke' else best_conc('ti_solo', TI_VRAM_CEILING, 2)
            if phase.homogeneous:
                # Both cards are A6000 -> run both at the A6000 best concurrency.
                run_level(phase, a6, corpus_path, n_files, a6, a6)
            elif phase.mode == 'gpu-scale':
                run_level(phase, ti, corpus_path, n_files, a6, ti)
            else:  # gpu-split
                conc = 2 if tier_name == 'smoke' else a6
                run_level(phase, conc, corpus_path, n_files, a6, ti)
        else:  # solo sequential (duration curve)
            run_level(phase, 1, corpus_path, n_files, 0, 0)

    log('==== ALL SELECTED PHASES COMPLETE ====')
    if RESULTS_TSV.exists():
        log('Results so far:\n' + RESULTS_TSV.read_text())
    log(f'Raw per-level data: {OUTROOT}/')
    log('Collate with: backend/venv/bin/python scripts/collate_benchmark.py')


if __name__ == '__main__':
    main()
