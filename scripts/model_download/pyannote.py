"""The PyAnnote diarization download group (gated Hugging Face repos)."""

import os
from pathlib import Path

from model_download.ui import (
    print_error,
    print_header,
    print_info,
    print_success,
    print_warning,
)


def validate_gated_model_access():
    """Validate access to gated PyAnnote models before attempting download"""
    print_info('Validating access to gated PyAnnote models...')
    print_info('')

    try:
        from huggingface_hub import HfApi

        hf_token = os.environ.get('HUGGINGFACE_TOKEN')
        if not hf_token:
            return False, 'No HuggingFace token provided'

        # List of REQUIRED gated models. community-1 IS gated (auto-approved, but the
        # agreement must still be accepted) and is the ONLY repo the app actually
        # downloads via whisperx's DiarizationPipeline (whisperx>=3.7 defaults to
        # "pyannote/speaker-diarization-community-1", see backend/app/transcription/
        # diarizer.py PYANNOTE_V4_MODEL and native_provision.py). segmentation-3.0 /
        # speaker-diarization-3.1 are NOT what either download group needs.
        gated_models = [
            'pyannote/speaker-diarization-community-1',
        ]

        api = HfApi()
        inaccessible_models = []
        access_errors = {}

        for model_id in gated_models:
            try:
                # Try to get model info - this will fail if user hasn't accepted the license
                api.model_info(model_id, token=hf_token)
                print_success(f'  ✓ Access confirmed: {model_id}')
            except Exception as e:
                error_msg = str(e)
                inaccessible_models.append(model_id)
                access_errors[model_id] = error_msg

                # Determine error type
                if '401' in error_msg or 'Unauthorized' in error_msg:
                    print_error(f'  ✗ Access DENIED (401 Unauthorized): {model_id}')
                    print_error('     You have NOT accepted the model agreement!')
                elif '403' in error_msg or 'Forbidden' in error_msg:
                    print_error(f'  ✗ Access DENIED (403 Forbidden): {model_id}')
                    print_error('     Token may not have required permissions')
                else:
                    print_error(f'  ✗ Cannot access: {model_id}')
                    print_error(f'     Error: {error_msg[:80]}')

        print_info('')

        if inaccessible_models:
            error_msg = f'Cannot access {len(inaccessible_models)} required gated model(s)'
            print_error('VALIDATION FAILED: Missing gated model access!')
            return False, (error_msg, access_errors)

        print_success('✅ All required gated models are accessible!')
        print_info('')
        return True, None

    except ImportError:
        # huggingface_hub might not be available, skip validation but warn
        print_info('  ⚠️  Skipping gated model validation (huggingface_hub not available)')
        print_info('  This is not recommended - validation may fail during download')
        print_info('')
        return True, None
    except Exception as e:
        # Unexpected error during validation - don't block but warn strongly
        print_error(f'  ⚠️  Could not validate gated models: {e}')
        print_info('  Proceeding anyway, but download may fail...')
        print_info('')
        return True, None


class GatedPipelineUnavailable(RuntimeError):
    """``Pipeline.from_pretrained`` returned None: pyannote's report of an unreadable gated repo.

    Its own class so the gated-access hint is never printed for an ordinary ``OSError``
    (a full or unwritable cache), which ``PermissionError`` would have been confused with.
    """


def pyannote_repo_plan():
    """The Hub repos every in-process PyAnnote path in the backend can load, from its own code.

    Derived from the backend's constants rather than copied, so renaming a model there
    changes what this fetches (``tests/unit/test_download_models_pyannote_plan.py``):

    * **required** -- ``diarizer.PYANNOTE_V4_MODEL`` (the in-process diarizer, and the
      source the diar-native export is made from) and the v4 speaker-embedding model
      (``EmbeddingModeService.get_embedding_model_name(MODE_V4)``), which
      ``SpeakerEmbeddingService`` loads with ``Model.from_pretrained`` whenever the
      diar-native sidecar is down or ``USE_NATIVE_SPEAKER_EMBEDDINGS=false``. That is a
      separate repo from the copy bundled inside community-1.
    * **optional** -- ``diarizer.PYANNOTE_V3_FALLBACK`` (loaded only when community-1 fails
      to load) and the v3 embedding model (used only by installs whose speaker index is
      still 512-d). Both are gated behind their own agreements, so a token without them
      leaves those paths unprovisioned and is reported, not fatal.

    Pipeline repos may name further model repos in their ``config.yaml``; those are added
    by ``pipeline_dependency_repos`` after the snapshot, from the file itself.

    Returns:
        ``(required, optional)`` lists of repo ids.
    """
    from app.services.embedding_mode_service import (
        MODE_V3,
        MODE_V4,
        EmbeddingModeService,
    )
    from app.transcription.diarizer import PYANNOTE_V3_FALLBACK, PYANNOTE_V4_MODEL

    required = [PYANNOTE_V4_MODEL, EmbeddingModeService.get_embedding_model_name(MODE_V4)]
    optional = [PYANNOTE_V3_FALLBACK, EmbeddingModeService.get_embedding_model_name(MODE_V3)]
    return required, optional


def pipeline_dependency_repos(snapshot_dir):
    """Hub repo ids a pipeline snapshot's ``config.yaml`` refers to (``$model/...`` excluded).

    ``speaker-diarization-community-1`` names only its own subfolders; ``-3.1`` names
    ``pyannote/segmentation-3.0`` and the WeSpeaker repo, which ``from_pretrained`` would
    otherwise fetch lazily at load time.
    """
    import re

    import yaml

    config = Path(snapshot_dir) / 'config.yaml'
    if not config.is_file():
        return []
    params = (yaml.safe_load(config.read_text()) or {}).get('pipeline', {}).get('params', {})
    repo = re.compile(r'^[A-Za-z0-9][\w.-]*/[\w.-]+$')
    return [v for v in params.values() if isinstance(v, str) and repo.match(v)]


def _snapshot_with_dependencies(repo_id, hf_token, snapshot_download, already=()):
    """Snapshot ``repo_id`` and every repo its config names, skipping ``already``."""
    fetched: list[str] = []
    pending = [repo_id]
    while pending:
        current = pending.pop(0)
        if current in fetched or current in already:
            continue
        print_info(f'  Fetching {current}...')
        path = snapshot_download(current, token=hf_token)
        fetched.append(current)
        pending.extend(pipeline_dependency_repos(path))
    return fetched


def fetch_diarization_pipeline(hf_token):
    """Fetch every PyAnnote repo the backend can load in-process, and prove community-1 loads.

    No inference runs. Each repo of ``pyannote_repo_plan()`` is fetched with
    ``snapshot_download`` (plus the repos a pipeline's ``config.yaml`` names), then
    community-1 is loaded with ``Pipeline.from_pretrained``, the exact call
    ``SpeakerDiarizer.load_model`` (``app/transcription/diarizer.py``) makes at runtime, so
    a snapshot the backend could not use fails here instead of on the first transcription.

    This used to transcribe and diarize 30 s of synthetic sine-wave audio through whisperx.
    whisperx's VAD found no speech in it, and ``FasterWhisperPipeline.transcribe`` handed an
    empty batch to ``transformers`` 5.x, whose ``Pipeline.__call__`` indexes ``inputs[0]``:
    ``IndexError: list index out of range``, deterministically, on every CPU run. Nothing in
    the backend uses whisperx (``Transcriber`` is faster-whisper's
    ``BatchedInferencePipeline``), and the Whisper weights are the ``whisperx`` group's job.

    Args:
        hf_token: Hugging Face token with the community-1 agreement accepted.

    Returns:
        ``(fetched, optional_unavailable)``: repo ids now in the cache, and
        ``{repo_id: error}`` for optional repos that could not be fetched.

    Raises:
        GatedPipelineUnavailable: ``from_pretrained`` returned None, which is how pyannote
            reports a gated repo the token cannot read.
    """
    from huggingface_hub import snapshot_download
    from pyannote.audio import Pipeline

    required, optional = pyannote_repo_plan()
    fetched: list[str] = []
    for repo_id in required:
        fetched += _snapshot_with_dependencies(repo_id, hf_token, snapshot_download, fetched)

    unavailable = {}
    for repo_id in optional:
        try:
            fetched += _snapshot_with_dependencies(repo_id, hf_token, snapshot_download, fetched)
        except Exception as exc:
            unavailable[repo_id] = f'{type(exc).__name__}: {str(exc)[:160]}'
            print_warning(
                f'  Optional {repo_id} not fetched ({type(exc).__name__}); the path that '
                'loads it will need network access. Accept its agreement on Hugging Face to '
                'include it.'
            )

    pipeline_repo = required[0]
    print_info('  Loading the pipeline from the cache (the backend load path, no inference)...')
    pipeline = Pipeline.from_pretrained(pipeline_repo, token=hf_token)
    if pipeline is None:
        raise GatedPipelineUnavailable(
            f'pyannote returned no pipeline for {pipeline_repo}; '
            'the token cannot read the gated repo'
        )
    del pipeline
    return fetched, unavailable


def download_pyannote_models():
    """Download the PyAnnote diarization pipeline the backend loads (no inference)."""
    print_header('Downloading PyAnnote Models')

    try:
        import logging
        import warnings

        # Suppress noisy-but-harmless library warnings that confuse users:
        # - Lightning checkpoint auto-upgrade notice (cosmetic, not an error)
        # - PyAnnote TF32 reproducibility advisory (we accept the trade-off)
        warnings.filterwarnings('ignore', message='.*Lightning automatically upgraded.*')
        warnings.filterwarnings('ignore', category=UserWarning, module='pyannote')
        logging.getLogger('pyannote').setLevel(logging.WARNING)
        logging.getLogger('lightning').setLevel(logging.ERROR)

        hf_token = os.environ.get('HUGGINGFACE_TOKEN')
        if not hf_token:
            print_error('HUGGINGFACE_TOKEN not set!')
            print_error('')
            print_error('A HuggingFace token is REQUIRED for speaker diarization models.')
            print_error('')
            print_error('To get your FREE token:')
            print_error('  1. Visit: https://huggingface.co/settings/tokens')
            print_error("  2. Click 'New token' and select 'Read' permissions")
            print_error('  3. Copy the token')
            print_error('')
            print_error('Then set it as an environment variable:')
            print_error('  export HUGGINGFACE_TOKEN=your_token_here')
            return {'pyannote': {'status': 'failed', 'error': 'No HuggingFace token'}}

        # Validate access to gated models BEFORE attempting download
        has_access, access_result = validate_gated_model_access()
        if not has_access:
            print_error('')
            print_error('=' * 80)
            print_error('❌ GATED MODEL ACCESS DENIED - DOWNLOAD CANNOT PROCEED')
            print_error('=' * 80)
            print_error('')
            print_error('⚠️  YOUR TOKEN DOES NOT HAVE ACCESS TO THE REQUIRED PYANNOTE MODEL')
            print_error('')
            print_error('This means you have NOT accepted the model user agreement.')
            print_error('')
            print_error('╔════════════════════════════════════════════════════════════════════╗')
            print_error('║  REQUIRED ACTION: Accept the model agreement on HuggingFace         ║')
            print_error('╚════════════════════════════════════════════════════════════════════╝')
            print_error('')
            print_error('Step 1: Visit the Speaker Diarization Model page')
            print_error('   URL: https://huggingface.co/pyannote/speaker-diarization-community-1')
            print_error("   → Look for the 'Agree and access repository' button")
            print_error('   → Click it to accept the terms')
            print_error('')
            print_error('Step 2: Wait 1-2 minutes for permissions to propagate')
            print_error('')
            print_error('Step 3: Run this script again:')
            print_error('   bash scripts/download-models.sh models')
            print_error('')
            print_error('=' * 80)
            print_error('')

            error_msg = access_result[0] if isinstance(access_result, tuple) else access_result
            return {'pyannote': {'status': 'failed', 'error': error_msg}}

        fetched, unavailable = fetch_diarization_pipeline(hf_token)
        print_success(f'  {len(fetched)} PyAnnote repo(s) downloaded; the pipeline loads')

        # The pipeline loaded from the cache, so every file it needs is present. (PyAnnote
        # 3.x+ stores weights in ~/.cache/huggingface/hub/, not the old
        # ~/.cache/torch/pyannote/ path, so directory-name checks are unreliable.)
        return {
            'pyannote': {
                'model': fetched[0],
                'repos': fetched,
                'optional_unavailable': unavailable,
                'status': 'downloaded',
                'validation': {'all_present': not unavailable},
            }
        }

    except Exception as e:
        error_msg = str(e)
        print_error(f'Failed to download PyAnnote models: {error_msg}')

        # Check if this looks like a gated model access error
        if (
            isinstance(e, GatedPipelineUnavailable)
            or 'cannot find the requested files' in error_msg.lower()
            or 'locate the file on the hub' in error_msg.lower()
            or '403' in error_msg
            or '401' in error_msg
        ):
            print_error('')
            print_error('=' * 70)
            print_error('⚠️  THIS LOOKS LIKE A GATED MODEL ACCESS ERROR!')
            print_error('=' * 70)
            print_error('')
            print_error("This error usually means you haven't accepted the model agreement.")
            print_error('')
            print_error('You MUST accept the PyAnnote gated model agreement:')
            print_error('')
            print_error('  Speaker Diarization Model:')
            print_error('     https://huggingface.co/pyannote/speaker-diarization-community-1')
            print_error("     → Click 'Agree and access repository' (if prompted)")
            print_error('')
            print_error('After accepting the agreement:')
            print_error('  • Wait 1-2 minutes for permissions to propagate')
            print_error('  • Run this script again: bash scripts/download-models.sh models')
            print_error('')
            print_error('=' * 70)

        return {'pyannote': {'status': 'failed', 'error': str(e)}}
