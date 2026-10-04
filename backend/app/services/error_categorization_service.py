"""Error Categorization Service for providing user-friendly error handling.

This service categorizes errors and provides helpful suggestions to users,
replacing the complex error handling logic that was previously in the frontend.

The service provides intelligent error classification and user guidance including:
- Pattern-based error categorization using predefined error types
- Context-aware user-friendly error messages
- Actionable suggestion lists for error resolution
- Retry eligibility determination based on error category
- Enhanced notification triggers for critical error types

Error reasons include:
- FILE_QUALITY: Issues with file corruption, format, or encoding
- NO_AUDIO_TRACK: The file has no audio track at all — nothing to transcribe
- NO_SPEECH: Audio contains no detectable speech content
- FORMAT_ISSUE: Codec, container, or technical format problems
- NETWORK_ERROR: Connectivity, download, or URL access issues
- PERMISSION_ERROR: Access control, DRM, or authentication failures
- PROCESSING_ERROR: Generic server-side processing failures
- INTERRUPTED: Server-side interruptions (a worker lost, out of memory) that outlasted
  every automatic retry
- UNCLASSIFIED: Unclassified errors with fallback handling

⚠️ No-raw-echo contract: every ``user_message`` and suggestion this service returns is a
FIXED sentence chosen by category. The raw exception text is NEVER embedded in any value
this module returns — it is a bug to add an f-string that re-inserts ``error_message`` into
a handler's output. The raw exception stays in the ERROR-level log and nowhere else.

``UserErrorReason`` is the USER-FACING vocabulary, distinct from
``app.utils.error_classification.ErrorCategory`` — that module's enum drives RETRY policy
(is this worth retrying, and how long to wait) and is never serialized to a client.

Classify ONCE, at the failure site (issue #959): a pipeline failure handler calls
``classify_failure(raw)`` while it still holds the raw exception, persists
``user_message`` to ``media_file.last_error_message`` / ``task.error_message`` and
``retry_category`` to ``media_file.error_category``. Retry policy then reads the stored
category column and never re-derives it from stored prose, so rewording a sentence here
cannot change retry behaviour. Read edges call ``get_error_info`` / ``error_fields_for``; a
stored fixed sentence maps back to its own reason by exact match (``_REASON_BY_MESSAGE``),
and legacy rows that still hold raw text fall through to the pattern match.
``scripts/audit-error-disclosure.py`` gates the read edges.

All error processing is designed to be non-breaking - if categorization fails,
the service gracefully falls back to generic error handling.

Example:
    Basic usage for categorizing an error:

    reason, message, suggestions = ErrorCategorizationService.categorize_error(error_msg)
    error_info = ErrorCategorizationService.get_error_info(error_msg)

Classes:
    UserErrorReason: Enum defining all supported user-facing error reasons.
    ErrorCategorizationService: Main service class for error processing.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.utils.error_classification import INFRASTRUCTURE_CATEGORIES
from app.utils.error_classification import ErrorCategory
from app.utils.error_classification import categorize_error as categorize_retry

logger = logging.getLogger(__name__)


class UserErrorReason(StrEnum):
    """User-facing error reasons for classification."""

    FILE_QUALITY = "file_quality"
    NO_AUDIO_TRACK = "no_audio_track"
    NO_SPEECH = "no_speech"
    FORMAT_ISSUE = "format_issue"
    PROCESSING_ERROR = "processing_error"
    INTERRUPTED = "interrupted"
    NETWORK_ERROR = "network_error"
    PERMISSION_ERROR = "permission_error"
    UNCLASSIFIED = "unclassified"


#: User-facing reasons that say the INPUT is unusable. Running the same file again cannot
#: succeed, so ``classify_failure`` gives them the permanent ``INVALID_MEDIA`` retry
#: category unless the raw text carries an infrastructure signal (see
#: ``INFRASTRUCTURE_CATEGORIES``). PERMISSION_ERROR is deliberately absent: "access denied"
#: is as often our own object store as a DRM-locked upload.
INPUT_ERROR_REASONS: frozenset[UserErrorReason] = frozenset(
    {
        UserErrorReason.FILE_QUALITY,
        UserErrorReason.NO_AUDIO_TRACK,
        UserErrorReason.NO_SPEECH,
        UserErrorReason.FORMAT_ISSUE,
    }
)


#: The PERMISSION_ERROR sub-cases that are a property of the upload itself.
_PROTECTED_INPUT_PATTERNS = ("drm", "encrypted", "password-protected", "password protected")


def _is_input_failure(reason: UserErrorReason, raw_error: str | None) -> bool:
    if reason in INPUT_ERROR_REASONS:
        return True
    if reason == UserErrorReason.PERMISSION_ERROR and raw_error:
        lowered = raw_error.lower()
        return any(pattern in lowered for pattern in _PROTECTED_INPUT_PATTERNS)
    return False


@dataclass(frozen=True)
class FailureClassification:
    """What a failure site persists instead of the raw exception (issue #959)."""

    retry_category: ErrorCategory
    reason: UserErrorReason
    user_message: str


class ErrorCategorizationService:
    """Service for categorizing errors and providing user suggestions."""

    # Error patterns for categorization
    NO_AUDIO_TRACK_PATTERNS = [
        "does not contain any audio",
        "no audio track",
        "contains no audio track",
        "no audio streams found",
        "does not contain audio",
    ]

    FILE_QUALITY_PATTERNS = [
        "no audio content",
        "corrupted",
        "unsupported format",
        "invalid format",
        "cannot decode",
        "file damaged",
        "unreadable",
        "malformed",
        # audio_processor.py / transcription/audio.py: an empty upload, a clip too short
        "is empty and contains no content",
        "too short to contain meaningful content",
    ]

    NO_SPEECH_PATTERNS = [
        "no speech",
        "only music",
        "background noise",
        "silence detected",
        "instrumental",
        "non-verbal",
        "inaudible",
    ]

    FORMAT_PATTERNS = [
        "codec not supported",
        "container format",
        "encoding error",
        "bitrate",
        "sample rate",
        "channels not supported",
        "format is not supported",
        "unsupported codec",
        "convert to a common format",
    ]

    NETWORK_PATTERNS = [
        "connection",
        "timeout",
        "network",
        "download failed",
        "url not accessible",
        "forbidden",
    ]

    PERMISSION_PATTERNS = [
        "permission denied",
        "access denied",
        "unauthorized",
        "drm",
        "protected content",
        "password-protected",
        "password protected",
        "drm-protected",
    ]

    @staticmethod
    def categorize_error(error_message: str | None) -> tuple[UserErrorReason, str, list[str]]:
        """Categorize an error and provide user-friendly information.

        This method analyzes error messages using pattern matching to classify
        errors into predefined reasons and provide contextual user guidance.

        Args:
            error_message: The raw error message to categorize. Can be None
                for unknown errors.

        Returns:
            Tuple containing:
            - UserErrorReason: The classified error reason
            - str: User-friendly error message for display (a FIXED sentence,
              never the raw error text)
            - list[str]: List of actionable suggestions for error resolution

        Note:
            Pattern matching is case-insensitive and uses substring matching
            for maximum flexibility. If no patterns match, the error is
            classified as UNCLASSIFIED with generic suggestions.

        Example:
            >>> reason, msg, suggestions = categorize_error("corrupted file")
            >>> print(reason)  # UserErrorReason.FILE_QUALITY
            >>> print(len(suggestions))  # 5 specific suggestions
        """
        if not error_message:
            return (
                UserErrorReason.UNCLASSIFIED,
                "An unknown error occurred during processing.",
                ["Try uploading the file again", "Contact support if the problem persists"],
            )

        # Security: Limit error message length to prevent memory issues
        if len(error_message) > 10000:
            logger.warning(f"Error message truncated from {len(error_message)} to 10000 characters")
            error_message = error_message[:10000]

        exact = _REASON_BY_MESSAGE.get(error_message.strip())
        if exact is not None:
            return exact()

        error_lower = error_message.lower()

        # Check for a missing audio track before the broader file-quality check —
        # audio_processor.py's "no audio track" sentences would otherwise fall
        # through to FILE_QUALITY, which carries the wrong suggestions and retry policy.
        if any(
            pattern in error_lower for pattern in ErrorCategorizationService.NO_AUDIO_TRACK_PATTERNS
        ):
            return ErrorCategorizationService._handle_no_audio_track_error()

        # Check for file quality issues
        if any(
            pattern in error_lower for pattern in ErrorCategorizationService.FILE_QUALITY_PATTERNS
        ):
            return ErrorCategorizationService._handle_file_quality_error()

        # Check for speech detection issues
        if any(pattern in error_lower for pattern in ErrorCategorizationService.NO_SPEECH_PATTERNS):
            return ErrorCategorizationService._handle_no_speech_error()

        # Check for format issues
        if any(pattern in error_lower for pattern in ErrorCategorizationService.FORMAT_PATTERNS):
            return ErrorCategorizationService._handle_format_error()

        # Check for network issues
        if any(pattern in error_lower for pattern in ErrorCategorizationService.NETWORK_PATTERNS):
            return ErrorCategorizationService._handle_network_error()

        # Check for permission issues
        if any(
            pattern in error_lower for pattern in ErrorCategorizationService.PERMISSION_PATTERNS
        ):
            return ErrorCategorizationService._handle_permission_error()

        # Generic processing error
        return ErrorCategorizationService._handle_generic_error()

    @staticmethod
    def _handle_file_quality_error() -> tuple[UserErrorReason, str, list[str]]:
        """Handle file quality/corruption errors."""
        return (
            UserErrorReason.FILE_QUALITY,
            "This file could not be read. It may be corrupted, incomplete, or not a valid "
            "media file.",
            [
                "Check if the file plays correctly on your device",
                "Try converting to MP3, WAV, or MP4 format",
                "Ensure the file isn't password protected or DRM-locked",
                "Consider re-recording if the original source is problematic",
                "Verify the file wasn't corrupted during download or transfer",
            ],
        )

    @staticmethod
    def _handle_no_audio_track_error() -> tuple[UserErrorReason, str, list[str]]:
        """Handle files that contain no audio track at all."""
        return (
            UserErrorReason.NO_AUDIO_TRACK,
            "This file has no audio track, so there is nothing to transcribe.",
            [
                "Upload a file that contains an audio track",
                "If this is a video, check that audio was included when it was exported",
                "Try uploading a different file to test",
            ],
        )

    @staticmethod
    def _handle_no_speech_error() -> tuple[UserErrorReason, str, list[str]]:
        """Handle no speech detected errors."""
        return (
            UserErrorReason.NO_SPEECH,
            "No speech was detected in this recording.",
            [
                "Ensure the file contains clear, audible speech",
                "Check if speech is too quiet or unclear",
                "Reduce background noise if possible",
                "Verify this isn't a music-only or instrumental file",
                "Try uploading a different section with clearer audio",
            ],
        )

    @staticmethod
    def _handle_format_error() -> tuple[UserErrorReason, str, list[str]]:
        """Handle format/encoding errors."""
        return (
            UserErrorReason.FORMAT_ISSUE,
            "This file's format or codec is not supported.",
            [
                "Convert to a supported format (MP3, WAV, MP4, M4A)",
                "Try re-encoding with standard settings",
                "Check if the file uses an uncommon codec",
                "Ensure the file extension matches the actual format",
                "Use a different audio/video converter tool",
            ],
        )

    @staticmethod
    def _handle_network_error() -> tuple[UserErrorReason, str, list[str]]:
        """Handle network/download errors."""
        return (
            UserErrorReason.NETWORK_ERROR,
            "The file could not be retrieved. This is usually temporary.",
            [
                "Check your internet connection",
                "Verify the URL is accessible and not expired",
                "Try the upload again in a few minutes",
                "Download the file locally first, then upload",
                "Contact the content provider if URL access issues persist",
            ],
        )

    @staticmethod
    def _handle_permission_error() -> tuple[UserErrorReason, str, list[str]]:
        """Handle permission/access errors."""
        return (
            UserErrorReason.PERMISSION_ERROR,
            "Access to this content was refused. It may be protected or require sign-in.",
            [
                "Ensure you have permission to access this content",
                "Check if the content is behind a paywall or login",
                "Verify the content isn't DRM-protected",
                "Try downloading the file manually first",
                "Contact the content owner for access permissions",
            ],
        )

    @staticmethod
    def _handle_interrupted_error() -> tuple[UserErrorReason, str, list[str]]:
        """Handle a server-side interruption that outlasted every automatic retry."""
        return (
            UserErrorReason.INTERRUPTED,
            "Processing was interrupted by a temporary server problem and still could not "
            "finish after several automatic retries.",
            [
                'Use the "Retry" button to try processing again',
                "If it keeps failing, ask your administrator to check the processing workers",
            ],
        )

    @staticmethod
    def _handle_generic_error() -> tuple[UserErrorReason, str, list[str]]:
        """Handle generic processing errors."""
        return (
            UserErrorReason.PROCESSING_ERROR,
            "Processing failed for this file.",
            [
                'Use the "Retry" button to try processing again',
                "Check the file format and quality",
                "Try uploading a different file to test",
                "Contact support if the problem persists",
                "Check system status for any ongoing issues",
            ],
        )

    @staticmethod
    def get_error_info(error_message: str | None) -> dict[str, Any]:
        """Get comprehensive error information for API responses.

        This method provides a complete error information package suitable
        for API responses, including categorization, user messages, and
        metadata for frontend handling.

        Args:
            error_message: The raw error message to process. Can be None.

        Returns:
            Dictionary containing:
            - category: String representation of the error reason
            - user_message: User-friendly error description (never the raw error)
            - suggestions: List of actionable resolution steps
            - is_retryable: Boolean indicating if the error is worth retrying

        Note:
            The is_retryable field helps frontends determine whether to show
            retry buttons or encourage users to fix the underlying issue first.
            The raw error message is deliberately NOT included here — it lives
            only in the ERROR log (issue #959).

        Example:
            >>> info = get_error_info("network timeout")
            >>> info['is_retryable']  # True
            >>> len(info['suggestions'])  # 5
        """
        category, user_message, suggestions = ErrorCategorizationService.categorize_error(
            error_message
        )

        return {
            "category": category.value,
            "user_message": user_message,
            "suggestions": suggestions,
            "is_retryable": category
            in [
                UserErrorReason.NETWORK_ERROR,
                UserErrorReason.PROCESSING_ERROR,
                UserErrorReason.INTERRUPTED,
                UserErrorReason.UNCLASSIFIED,
            ],
        }

    @staticmethod
    def classify_failure(raw_error: str | None) -> FailureClassification:
        """Classify a raw failure ONCE, at the failure site, into everything that is stored.

        The single entry point for a failure handler: it yields the retry-policy category
        (derived from the RAW text, which carries the signal) and the fixed user-facing
        sentence that is persisted in place of the raw text. Callers log the raw exception
        themselves and store only ``user_message`` and ``retry_category``.
        """
        reason, user_message, _ = ErrorCategorizationService.categorize_error(raw_error)
        retry_category = categorize_retry(raw_error or "")
        if _is_input_failure(reason, raw_error) and retry_category not in INFRASTRUCTURE_CATEGORIES:
            retry_category = ErrorCategory.INVALID_MEDIA
        return FailureClassification(
            retry_category=retry_category,
            reason=reason,
            user_message=user_message,
        )

    @staticmethod
    def interrupted_message() -> str:
        """The fixed sentence stored when transient retries are exhausted."""
        return ErrorCategorizationService._handle_interrupted_error()[1]

    @staticmethod
    def error_fields_for(media_file: Any) -> dict[str, Any] | None:
        """Wire fields for a failed file, or None when the file has not failed.

        The one read edge from ``media_file.last_error_message`` to a file response —
        shared by the file-detail endpoint and ``FormattingService`` so the sanitization
        cannot drift between them.
        """
        from app.models.media import FileStatus

        if media_file.status != FileStatus.ERROR:
            return None
        stored = media_file.last_error_message
        info = ErrorCategorizationService.get_error_info(str(stored) if stored else None)
        return {
            "error_reason": info["category"],
            "error_suggestions": info["suggestions"],
            "user_message": info["user_message"],
            "is_retryable": info["is_retryable"],
        }

    @staticmethod
    def user_message_for(stored_error: str | None) -> str | None:
        """The client-safe sentence for a stored error column, or None when there is none."""
        if not stored_error:
            return None
        return str(ErrorCategorizationService.get_error_info(str(stored_error))["user_message"])

    @staticmethod
    def should_show_enhanced_notification(error_message: str | None) -> bool:
        """Determine if an enhanced error notification should be shown.

        This method identifies errors that warrant special attention from users,
        such as file quality issues or speech detection problems that require
        user action rather than simple retries.

        Args:
            error_message: The error message to evaluate for notification level.

        Returns:
            True if enhanced notification is recommended, False for standard
            notifications. Enhanced notifications typically include additional
            guidance and more prominent display.

        Enhanced notifications are triggered for:
            - File quality and corruption issues
            - Speech detection failures
            - Any error that requires user intervention to resolve

        Regular notifications are used for:
            - Network errors (temporary)
            - Processing errors (retryable)
            - Generic system errors

        Example:
            >>> should_show_enhanced_notification("corrupted file")  # True
            >>> should_show_enhanced_notification("network timeout")  # False
        """
        if not error_message:
            return False

        error_lower = error_message.lower()

        # Show enhanced notifications for quality and speech issues
        quality_issues = any(
            pattern in error_lower for pattern in ErrorCategorizationService.FILE_QUALITY_PATTERNS
        )
        speech_issues = any(
            pattern in error_lower for pattern in ErrorCategorizationService.NO_SPEECH_PATTERNS
        )

        return quality_issues or speech_issues


_FIXED_MESSAGE_HANDLERS: tuple[Callable[[], tuple[UserErrorReason, str, list[str]]], ...] = (
    ErrorCategorizationService._handle_file_quality_error,
    ErrorCategorizationService._handle_no_audio_track_error,
    ErrorCategorizationService._handle_no_speech_error,
    ErrorCategorizationService._handle_format_error,
    ErrorCategorizationService._handle_network_error,
    ErrorCategorizationService._handle_permission_error,
    ErrorCategorizationService._handle_interrupted_error,
    ErrorCategorizationService._handle_generic_error,
)

# A persisted fixed sentence must read back as the reason it was written for. Several of
# the sentences do not contain their own category's substring patterns (the network one
# says "could not be retrieved", not "network"), so without this exact-match table a stored
# FORMAT_ISSUE would read back as a generic PROCESSING_ERROR.
_REASON_BY_MESSAGE: dict[str, Callable[[], tuple[UserErrorReason, str, list[str]]]] = {
    handler()[1]: handler for handler in _FIXED_MESSAGE_HANDLERS
}
