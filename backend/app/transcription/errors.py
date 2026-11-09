"""Exception families the transcription engine absorbs on purpose, named once."""

from __future__ import annotations

from pickle import UnpicklingError  # nosec B403 - exception type only, nothing is unpickled here

#: What loading a diarization/ASR model raises: HF hub/network/auth failures (OSError, incl.
#: PermissionError and TimeoutError), checkpoint-load failures (RuntimeError, which is also
#: what torch.cuda.OutOfMemoryError and the CUDA driver raise, plus pickle errors), bad config
#: (ValueError, KeyError) and a missing optional dependency (ImportError).
MODEL_LOAD_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    KeyError,
    ImportError,
    UnpicklingError,
)
