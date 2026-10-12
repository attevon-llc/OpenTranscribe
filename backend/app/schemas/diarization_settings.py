"""Wire contract for the per-user pyannote.ai diarization credential (issue #1204).

The key is write-only: it is accepted by ``PyannoteCredentialUpdate`` /
``PyannoteConnectionTestRequest`` and returned by nothing. ``PyannoteCredentialStatus``
reports whether one is stored and how its last test went.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

#: Bounds for a pasted API key. Vendor keys are a few dozen characters; the cap stops a
#: resource-exhaustion paste, the floor catches an obviously truncated one.
PYANNOTE_KEY_MIN_LEN = 8
PYANNOTE_KEY_MAX_LEN = 512
_KEY_SHAPE = re.compile(r"^[\x21-\x7e]+$")


def validate_pyannote_key(value: str) -> str:
    """Trim surrounding whitespace, then require printable ASCII with no inner spaces.

    Called by the endpoint, NOT as a pydantic validator: FastAPI's 422 body echoes the
    rejected ``input``, which would put a mistyped secret into the response and any log
    that records it. The endpoint turns the ``ValueError`` into a 422 with a fixed message.
    """
    key = value.strip()
    if not PYANNOTE_KEY_MIN_LEN <= len(key) <= PYANNOTE_KEY_MAX_LEN:
        raise ValueError(
            f"API key must be {PYANNOTE_KEY_MIN_LEN} to {PYANNOTE_KEY_MAX_LEN} characters"
        )
    if not _KEY_SHAPE.match(key):
        raise ValueError("API key must not contain spaces or non-printable characters")
    return key


class PyannoteCredentialUpdate(BaseModel):
    """``PUT /user-settings/diarization/pyannote``: store (or replace) the key."""

    api_key: str = Field(..., description="pyannote.ai API key. Stored encrypted, never returned.")


class PyannoteConnectionTestRequest(BaseModel):
    """``POST /user-settings/diarization/pyannote/test``: omit ``api_key`` to test the saved one."""

    api_key: str | None = Field(
        default=None, description="A key to test without saving it. Omit to test the saved key."
    )


class PyannoteCredentialStatus(BaseModel):
    """What the client may know about the stored credential. Never the key itself."""

    configured: bool = Field(..., description="A key is stored for the current user")
    locked: bool = Field(
        ...,
        description=(
            "The deployment manages the speaker-detection source, so the key cannot be "
            "changed here (issue #1109)"
        ),
    )
    test_status: str | None = Field(
        default=None, description="'success' or 'failed' from the last saved-key test"
    )
    test_message: str | None = None
    last_tested: datetime | None = None
    updated_at: datetime | None = None


class PyannoteCredentialDeleted(BaseModel):
    """``DELETE /user-settings/diarization/pyannote``."""

    deleted: bool
    diarization_source: str = Field(
        ..., description="The user's speaker-detection source after the delete"
    )
    source_reverted: bool = Field(
        ..., description="The source was pyannote and has been reset to the default"
    )


PyannoteTestCode = Literal["connected", "rejected", "unreachable", "error"]


class PyannoteConnectionTestResult(BaseModel):
    """Outcome of a connection test. ``message`` is a fixed sentence, never vendor text."""

    success: bool
    code: PyannoteTestCode = Field(..., description="Stable outcome code for translated UI copy")
    message: str
    response_time_ms: int
