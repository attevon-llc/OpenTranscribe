"""A file's per-file transcription request, stored on the row and replayed on re-runs.

``dispatch_transcription_pipeline`` is reached by two kinds of caller. A *fresh request*
(upload, reprocess, watch import, URL import) states what the user wants for this file, and
``None`` there means "use my saved setting". A *re-run* (retry, recovery sweep, a stuck-task
restart) states nothing, and must not quietly turn the file's model, speaker range or
skipped diarization back into defaults (issue #1203). The first kind stores its answer on
the row; the second reads it back. Resolution order is unchanged: per-file value, then the
user's saved setting (``user_settings.resolve_speaker_range``), then the deployment default.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.media import MediaFile


@dataclass(frozen=True)
class DispatchOptions:
    """The per-file options a dispatch actually uses."""

    whisper_model: str | None
    min_speakers: int | None
    max_speakers: int | None
    num_speakers: int | None
    disable_diarization: bool | None
    diarization_source: str | None


def store_requested_options(
    media_file: MediaFile,
    *,
    whisper_model: str | None,
    min_speakers: int | None,
    max_speakers: int | None,
    num_speakers: int | None,
    disable_diarization: bool | None,
) -> None:
    """Record a fresh request on the row, overwriting an earlier one (``None`` included).

    ``None`` overwrites on purpose: a reprocess that asks for the default model must not leave
    an older "tiny" behind for the next retry to replay.
    """
    media_file.requested_whisper_model = whisper_model  # type: ignore[assignment]
    media_file.requested_min_speakers = min_speakers  # type: ignore[assignment]
    media_file.requested_max_speakers = max_speakers  # type: ignore[assignment]
    media_file.requested_num_speakers = num_speakers  # type: ignore[assignment]
    media_file.requested_disable_diarization = disable_diarization  # type: ignore[assignment]


def resolve_dispatch_options(
    media_file: MediaFile,
    *,
    reuse_requested_options: bool,
    whisper_model: str | None,
    min_speakers: int | None,
    max_speakers: int | None,
    num_speakers: int | None,
    disable_diarization: bool | None,
    diarization_source: str | None,
) -> DispatchOptions:
    """Merge a dispatch's arguments with the file's stored request, and store the result.

    An explicit argument always wins. With ``reuse_requested_options`` every argument left
    ``None`` is read back from the row (a re-run, or an upload whose ``/complete`` did not
    repeat what ``/prepare`` recorded); without it ``None`` stays ``None`` and so replaces an
    older request. A stored "diarization skipped" becomes ``diarization_source="off"``; it is
    never rewritten to ``provider``, which is what ``disable_diarization=False`` used to mean
    and which silently dropped a ``local`` or ``pyannote`` choice.
    """
    requested_off = disable_diarization
    if requested_off is None and diarization_source is not None:
        requested_off = diarization_source == "off"

    if reuse_requested_options:
        from app.core.locked_settings import effective_whisper_model

        if whisper_model is None:
            # A model stored before the deployment locked model choice must not apply.
            whisper_model = effective_whisper_model(media_file.requested_whisper_model, None)
        if min_speakers is None:
            min_speakers = media_file.requested_min_speakers
        if max_speakers is None:
            max_speakers = media_file.requested_max_speakers
        if num_speakers is None:
            num_speakers = media_file.requested_num_speakers
        if requested_off is None:
            requested_off = media_file.requested_disable_diarization

    store_requested_options(
        media_file,
        whisper_model=whisper_model,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        num_speakers=num_speakers,
        disable_diarization=requested_off,
    )

    # Not only a request but a fact: a file that already ran with diarization off re-runs so.
    if (
        reuse_requested_options
        and disable_diarization is None
        and diarization_source is None
        and (requested_off or media_file.diarization_disabled)
    ):
        diarization_source = "off"
    return DispatchOptions(
        whisper_model,
        min_speakers,
        max_speakers,
        num_speakers,
        disable_diarization,
        diarization_source,
    )


def apply_requested_options(media_file: MediaFile, options: dict | None) -> None:
    """Record the options an ingest request carried (URL import, playlist item).

    ``options`` holds any of ``whisper_model`` and ``min/max/num_speakers``; absent keys
    stay ``None`` ("use my saved setting"). The pipeline reads them back when the download
    finishes and dispatches transcription (``reuse_requested_options``), so a value the
    user entered survives the download hop and any later retry.
    """
    options = options or {}
    store_requested_options(
        media_file,
        whisper_model=options.get("whisper_model"),
        min_speakers=options.get("min_speakers"),
        max_speakers=options.get("max_speakers"),
        num_speakers=options.get("num_speakers"),
        disable_diarization=None,
    )
