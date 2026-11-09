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
import os
import sys
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


def main():
    """Main execution"""
    parser = argparse.ArgumentParser(description='Download OpenTranscribe model assets')
    parser.add_argument(
        '--only',
        action='append',
        choices=sorted(DOWNLOAD_GROUPS),
        help='Fetch only this group; repeatable. Default: every group.',
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

    results = {}

    for group in selected:
        results.update(DOWNLOAD_GROUPS[group]())

    # Create manifest
    manifest = create_manifest(results)

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
