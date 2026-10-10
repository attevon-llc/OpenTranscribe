"""Which per-file Whisper models the pipeline can actually honour (issue #1202).

The GPU workers load ONE model, the admin-pinned ``asr.local_model``; only the lightweight
models (tiny/base) are routed to a separate CPU worker. Any other valid-looking model name
used to be accepted and then ignored, so the API answered 2xx for a request it would not
carry out. Rejecting it up front is the honest answer.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi import status

from app.transcription.config import LIGHTWEIGHT_MODELS
from app.transcription.config import TranscriptionConfig


def deployment_model_name() -> str:
    """The model the GPU workers serve (admin setting, then env, then built-in default)."""
    return TranscriptionConfig._resolve_model_name()


def require_servable_whisper_model(model: str | None) -> str | None:
    """Return ``model`` if the pipeline can run it, else raise a 422.

    ``None`` (use the deployment's model) and the lightweight models always pass. Call it
    on the value that survives ``effective_whisper_model``: when the deployment has locked
    model choice the client's value is already discarded and there is nothing to reject.
    """
    if not model or model in LIGHTWEIGHT_MODELS:
        return model
    from app.services.asr.model_discovery import resolve_loadable_model_name

    served = deployment_model_name()
    if model in (served, resolve_loadable_model_name(served)) or (
        resolve_loadable_model_name(model) == resolve_loadable_model_name(served)
    ):
        return model
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=(
            f"Whisper model '{model}' is not available for per-file selection. This "
            f"deployment runs '{served}' on the GPU; the only other choices are the "
            f"lightweight CPU models ({', '.join(sorted(LIGHTWEIGHT_MODELS))}). Omit "
            "whisper_model to use the deployment model."
        ),
    )
