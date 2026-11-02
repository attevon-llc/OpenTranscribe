"""Argument parsing and dispatch for the direct PyAnnote benchmark."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pyannote_direct.config import DEFAULT_MODEL, DEFAULT_OPTIMIZED_SRC, SCRIPT_DIR, TEST_FILES
from pyannote_direct.driver import run_benchmark
from pyannote_direct.summary import compare_results


def main() -> None:
    parser = argparse.ArgumentParser(description='Direct PyAnnote diarization benchmark')
    parser.add_argument(
        '--variant',
        choices=['stock', 'optimized', 'optimized_cpu'],
        help='Which PyAnnote variant to benchmark',
    )
    parser.add_argument('--files', nargs='*', help='Specific file labels (default: all 5)')
    parser.add_argument(
        '--device',
        default='cuda',
        choices=['cuda', 'mps', 'cpu'],
        help='Device (cuda, mps, cpu)',
    )
    parser.add_argument('--gpu-index', type=int, default=0, help='GPU device index (cuda only)')
    parser.add_argument(
        '--runs',
        type=int,
        default=1,
        help='Number of runs per file (default 1; Phase A-style statistical: 5)',
    )
    parser.add_argument(
        '--tag',
        default=None,
        help='Label included in output filenames and report metadata',
    )
    parser.add_argument(
        '--rttm-out',
        default=None,
        help='Directory to write RTTM output per run (for DER scoring downstream)',
    )
    parser.add_argument(
        '--profiler',
        action='store_true',
        help='Wrap run 0 in torch.profiler and export a Chrome trace',
    )
    parser.add_argument(
        '--model',
        default=DEFAULT_MODEL,
        help=f'Diarization model id (default: {DEFAULT_MODEL})',
    )
    parser.add_argument(
        '--optimized-src',
        default=os.environ.get('PYANNOTE_OPTIMIZED_SRC'),
        help=(
            'Path to the canonical davidamacey/pyannote-audio fork src (default: '
            f'{DEFAULT_OPTIMIZED_SRC}).'
        ),
    )
    parser.add_argument(
        '--torch-compile',
        action='store_true',
        help=(
            'After loading the pipeline, wrap the segmentation and embedding '
            'models in torch.compile() (Phase 2.1 follow-up). First run pays a '
            'JIT warmup cost; subsequent runs should be ~5-15%% faster if '
            'Dynamo does not hit a graph break.'
        ),
    )
    parser.add_argument(
        '--segmentation-step',
        type=float,
        default=None,
        help=(
            'Override the segmentation sliding-window step (default ~0.1 = '
            '90%% overlap). Higher values make segmentation N× faster at a '
            'DER cost — Phase 4 measures this trade-off.'
        ),
    )
    parser.add_argument(
        '--both',
        action='store_true',
        help='Run stock then optimized back-to-back and compare',
    )
    parser.add_argument(
        '--compare',
        nargs=2,
        metavar=('A', 'B'),
        help='Compare two saved result sets (e.g., --compare stock optimized)',
    )
    args = parser.parse_args()

    if args.compare:
        compare_results(args.compare[0], args.compare[1])
        return

    file_labels = args.files or TEST_FILES
    rttm_out = Path(args.rttm_out) if args.rttm_out else None
    optimized_src = Path(args.optimized_src) if args.optimized_src else None

    if args.both:
        import subprocess

        script = str(SCRIPT_DIR / 'benchmark-pyannote-direct.py')
        base_args = [sys.executable, '-W', 'ignore', script]
        extra = []
        if args.files:
            extra += ['--files'] + args.files
        extra += [
            '--device',
            args.device,
            '--gpu-index',
            str(args.gpu_index),
            '--runs',
            str(args.runs),
            '--model',
            args.model,
        ]
        if args.tag:
            extra += ['--tag', args.tag]
        if args.rttm_out:
            extra += ['--rttm-out', args.rttm_out]
        if args.profiler:
            extra += ['--profiler']

        print('=' * 60)
        print('  RUNNING STOCK BASELINE (subprocess)')
        print('=' * 60)
        subprocess.run(base_args + ['--variant', 'stock'] + extra, check=True)

        print('\n\n')
        print('=' * 60)
        print('  RUNNING OPTIMIZED (subprocess)')
        print('=' * 60)
        opt_env = os.environ.copy()
        opt_src_str = str(optimized_src or DEFAULT_OPTIMIZED_SRC)
        opt_env['PYTHONPATH'] = opt_src_str + ':' + opt_env.get('PYTHONPATH', '')
        if args.optimized_src:
            opt_env['PYANNOTE_OPTIMIZED_SRC'] = str(optimized_src)
            extra += ['--optimized-src', str(optimized_src)]
        subprocess.run(base_args + ['--variant', 'optimized'] + extra, env=opt_env, check=True)

        print('\n\n')
        compare_results('stock', 'optimized')
        return

    if not args.variant:
        parser.print_help()
        sys.exit(1)

    onnx_cpu = args.variant == 'optimized_cpu'
    device = 'cpu' if onnx_cpu else args.device
    run_benchmark(
        args.variant,
        file_labels,
        device,
        args.gpu_index,
        onnx_cpu=onnx_cpu,
        runs=args.runs,
        tag=args.tag,
        rttm_out=rttm_out,
        profiler=args.profiler,
        model_id=args.model,
        optimized_src=optimized_src,
        torch_compile=args.torch_compile,
        segmentation_step=args.segmentation_step,
    )
