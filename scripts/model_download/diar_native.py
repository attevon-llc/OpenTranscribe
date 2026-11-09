"""The diar-native (ONNX/PLDA) download group; delegates provisioning to the backend."""

from model_download.ui import print_error, print_header, print_info, print_success


def download_diar_native_models():
    """Provision the native diarizer's (`diar-server`) ONNX/PLDA model set.

    Delegates entirely to `app.transcription.native_provision.ensure_native_models` — the
    SAME function `main.py`'s FastAPI lifespan calls at backend startup — rather than
    re-implementing the model set, timeout, exit-code table, remedies, and the
    blank-`HF_ENDPOINT` scrub a second time here. There is exactly one implementation of
    diar-native provisioning; this is a caller of it, not a fork of it. See that module's
    docstring for why the export logic itself lives in `diar-server`, not in either Python
    caller. Note there is deliberately no `DEPLOYMENT_MODE=lite` skip anywhere in that chain
    (issue #654) — `requirements-lite.txt` now installs the export toolchain too, so a lite
    install provisions itself exactly like the full image.

    This works because this script runs INSIDE the backend container:
    `download-models.sh`'s `docker run` mounts this file at `/app/download-models.py` and
    runs it against `davidamacey/opentranscribe-backend` (the full image, not `-lite` — see
    `resolve_downloader_image()`), so `app.transcription.native_provision` is importable
    here exactly as it is from the FastAPI process. It also means the target directory,
    model set, and timeout all resolve identically to a real backend boot, with no separate
    config surface for this script to drift from.
    """
    print_header('Provisioning Native Diarization Models (diar-server)')

    try:
        from app.transcription.native_provision import ensure_native_models
    except ImportError as exc:
        # Degrade, don't crash: one group's absent dependency should not fail every other
        # group this run downloads. This is not "should never happen" -- it is exactly what
        # happens on an image built before this module existed, or a run of this script
        # outside the backend container it is designed for.
        print_error(f'Cannot import app.transcription.native_provision: {exc}')
        print_error(
            'This group only works when run inside the OpenTranscribe backend container -- '
            'see scripts/download-models.sh, which does this automatically. Skipping.'
        )
        return {
            'diar_native': {
                'status': 'skipped',
                'reason': f'native_provision unavailable: {exc}',
            }
        }

    result = ensure_native_models()

    if result.status == 'ok':
        verb = 'already provisioned' if result.already_provisioned else 'provisioned'
        print_success(
            f'diar-native models {verb} at {result.models_dir} ({result.duration_s:.1f}s)'
        )
        return {'diar_native': {'status': 'downloaded', 'models_dir': result.models_dir}}

    if result.status == 'skipped':
        print_info(f'diar-native provisioning skipped: {result.reason}')
        return {'diar_native': {'status': 'skipped', 'reason': result.reason}}

    print_error(f'diar-native provisioning failed: {result.reason}')
    return {
        'diar_native': {
            'status': 'failed',
            'error': result.reason,
            'exit_code': result.exit_code,
        }
    }
