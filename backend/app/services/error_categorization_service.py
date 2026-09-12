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
- UNCLASSIFIED: Unclassified errors with fallback handling

⚠️ No-raw-echo contract: every ``user_message`` and suggestion this service returns is a
FIXED sentence chosen by category. The raw exception text is NEVER embedded in any value
this module returns — it is a bug to add an f-string that re-inserts ``error_message`` into
a handler's output. The raw message stays server-side (``media_file.last_error_message`` and
the ERROR-level log), which is where `task_detection_service.py` reads it for OOM detection.

``UserErrorReason`` is the USER-FACING vocabulary, distinct from
``app.utils.error_classification.ErrorCategory`` — that module's enum drives RETRY policy
(is this worth retrying, and how long to wait) and is never serialized to a client.

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
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class UserErrorReason(StrEnum):
    """User-facing error reasons for classification."""

    FILE_QUALITY = "file_quality"
    NO_AUDIO_TRACK = "no_audio_track"
    NO_SPEECH = "no_speech"
    FORMAT_ISSUE = "format_issue"
    PROCESSING_ERROR = "processing_error"
    NETWORK_ERROR = "network_error"
    PERMISSION_ERROR = "permission_error"
    UNCLASSIFIED = "unclassified"


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
            The raw error message is deliberately NOT included here — it stays
            server-side in `media_file.last_error_message` and the ERROR log.

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
                UserErrorReason.UNCLASSIFIED,
            ],
        }

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
