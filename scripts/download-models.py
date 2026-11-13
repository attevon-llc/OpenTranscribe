#!/usr/bin/env python3
"""
Download all required AI models for OpenTranscribe offline packaging.

This script downloads:
- The local Whisper model (from the model catalog)
- PyAnnote speaker diarization models
- NLTK data files (punkt_tab tokenizer)
- Sentence-Transformers models (all-MiniLM-L6-v2)
- wav2vec2 gender classifier (speaker attribute detection)

Models are cached to standard locations and a manifest is created. The per-group
downloaders live in the sibling ``model_download`` package; this file keeps ``main()``.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def _load_package():
    """Import the sibling ``model_download`` package, whether run or loaded by path.

    The filename has a hyphen, so this script is run (or loaded by tests) by path; put the
    package's parent directory on ``sys.path`` explicitly instead of relying on the
    interpreter doing it. Torch is patched first: it MUST happen before any ML library import.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from model_download import groups, manifest, torch_compat, ui

    torch_compat.apply_torch_patches()
    return groups, manifest, ui


_groups, _manifest, _ui = _load_package()
DOWNLOAD_GROUPS = _groups.DOWNLOAD_GROUPS
GROUPS_WITHOUT_HF_TOKEN = _groups.GROUPS_WITHOUT_HF_TOKEN
create_manifest = _manifest.create_manifest
print_error, print_header, print_info, print_success, print_warning = (
    _ui.print_error,
    _ui.print_header,
    _ui.print_info,
    _ui.print_success,
    _ui.print_warning,
)


def _pump_prefixed(stream, prefix):
    """Copy a child's output line by line, tagged so overlapping output stays attributable."""
    for line in stream:
        print(f'{prefix} {line}', end='', flush=True)


def _start_diar_native_child(results_path):
    """Run the diar-native group as a child of this script, exactly the per-group path
    release scenario D proves (``--only diar-native``), so it overlaps the other groups
    without sharing torch patches or globals with this process."""
    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            '--only',
            'diar-native',
            '--results-out',
            results_path,
            '--no-manifest',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    pump = threading.Thread(target=_pump_prefixed, args=(proc.stdout, '[diar-native]'), daemon=True)
    pump.start()
    return proc, pump


def _run_groups(selected):
    """Run the selected groups; return the merged per-model results.

    diar-native exports from the pyannote weights, so when both are selected pyannote runs
    first and diar-native then overlaps every remaining group. A child that dies without
    writing results is reported as a failed diar-native entry (the exit-1 contract holds).
    """
    results = {}
    overlap = 'pyannote' in selected and 'diar-native' in selected
    child = None
    results_path = None
    if overlap:
        for group in selected:
            if group == 'pyannote':
                results.update(DOWNLOAD_GROUPS[group]())
        fd, results_path = tempfile.mkstemp(prefix='diar-native-', suffix='.json')
        os.close(fd)
        child = _start_diar_native_child(results_path)
    for group in selected:
        if overlap and group in ('pyannote', 'diar-native'):
            continue
        results.update(DOWNLOAD_GROUPS[group]())
    if child:
        proc, pump = child
        rc = proc.wait()
        pump.join()
        try:
            child_results = json.loads(Path(results_path).read_text())
        except (OSError, ValueError):
            child_results = {}
        finally:
            Path(results_path).unlink(missing_ok=True)
        if rc != 0 and not any(v.get('status') == 'failed' for v in child_results.values()):
            child_results['diar-native'] = {
                'status': 'failed',
                'error': f'diar-native subprocess exited {rc} without a failed result',
            }
        results.update(child_results)
    return results


def main():
    """Main execution"""
    parser = argparse.ArgumentParser(description='Download OpenTranscribe model assets')
    parser.add_argument(
        '--only',
        action='append',
        choices=sorted(DOWNLOAD_GROUPS),
        help='Fetch only this group; repeatable. Default: every group.',
    )
    parser.add_argument(
        '--results-out', help='Write the per-model results as JSON to this path (internal).'
    )
    parser.add_argument(
        '--no-manifest', action='store_true', help='Skip the manifest (internal; parent writes it).'
    )
    args = parser.parse_args()

    selected = args.only or list(DOWNLOAD_GROUPS)

    print_header('OpenTranscribe Model Downloader')
    if args.only:
        print_info(f'Selected group(s): {", ".join(selected)}')
    else:
        print_info('This script will download all required AI models')
    print_info('Models will be cached for offline packaging\n')

    # Check for required environment variables
    if not set(selected) <= GROUPS_WITHOUT_HF_TOKEN and not os.environ.get('HUGGINGFACE_TOKEN'):
        print_error('HUGGINGFACE_TOKEN environment variable not set!')
        print_info('Get your token at: https://huggingface.co/settings/tokens')
        sys.exit(1)

    results = _run_groups(selected)

    if args.results_out:
        Path(args.results_out).write_text(json.dumps(results))

    # Create manifest (a child run by the parent leaves it to the parent: one manifest)
    manifest = {'cache': {'total_size_gb': 0}} if args.no_manifest else create_manifest(results)

    # Summary
    print_header('Download Summary')

    failed = [k for k, v in results.items() if v.get('status') == 'failed']
    succeeded = [k for k, v in results.items() if v.get('status') == 'downloaded']

    partial = [k for k, v in results.items() if v.get('status') == 'partial']
    if partial:
        print_warning(f'Partially downloaded (some assets missing): {", ".join(partial)}')

    if failed:
        print_error(f'Failed to download: {", ".join(failed)}')
        for model in failed:
            print_error(f'  {model}: {results[model].get("error", "Unknown error")}')
        sys.exit(1)

    print_success(f'Successfully downloaded {len(succeeded)} model(s)')
    print_success(f'Total cache size: {manifest["cache"]["total_size_gb"]} GB')

    print('\nCache locations:')
    for name, info in manifest['cache'].items():
        if name != 'total_size_gb':
            print(f'  {name}: {info["path"]} ({info.get("size_gb", 0)} GB)')

    print_success('\n✨ All models downloaded successfully!')


if __name__ == '__main__':
    main()
