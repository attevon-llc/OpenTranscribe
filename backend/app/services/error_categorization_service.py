"""Error Categorization Service for providing user-friendly error handling.

This service categorizes errors and provides helpful suggestions to users,
replacing the complex error handling logic that was previously in the frontend.

The service provides intelligent error classification and user guidance including:
- Pattern-based error categorization using predefined error types
- Context-aware user-friendly error messages
- Actionable suggestion lists for error resolution
- Retry eligibility determination based on error category
- Enhanced notification triggers for critical error types

Error categories include:
- FILE_QUALITY: Issues with file corruption, format, or encoding
- NO_SPEECH: Audio contains no detectable speech content
- FORMAT_ISSUE: Codec, container, or technical format problems
- NETWORK_ERROR: Connectivity, download, or URL access issues
- PERMISSION_ERROR: Access control, DRM, or authentication failures
- PROCESSING_ERROR: Generic server-side processing failures
- UNKNOWN: Unclassified errors with fallback handling

All error processing is designed to be non-breaking - if categorization fails,
the service gracefully falls back to generic error handling.

Example:
    Basic usage for categorizing an error:

    category, message, suggestions = ErrorCategorizationService.categorize_error(error_msg)
    error_info = ErrorCategorizationService.get_error_info(error_msg)

Classes:
    ErrorCategory: Enum defining all supported error categories.
    ErrorCategorizationService: Main service class for error processing.
"""

import logging
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class ErrorCategory(StrEnum):
    """Error categories for user-friendly classification."""

    FILE_QUALITY = "file_quality"
    NO_SPEECH = "no_speech"
    FORMAT_ISSUE = "format_issue"
    PROCESSING_ERROR = "processing_error"
    NETWORK_ERROR = "network_error"
    PERMISSION_ERROR = "permission_error"
    UNKNOWN = "unknown"


class ErrorCategorizationService:
    """Service for categorizing errors and providing user suggestions."""

    # Error patterns for categorization
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
    ]

    @staticmethod
    def categorize_error(error_message: str | None) -> tuple[ErrorCategory, str, list[str]]:
        """Categorize an error and provide user-friendly information.

        This method analyzes error messages using pattern matching to classify
        errors into predefined categories and provide contextual user guidance.

        Args:
            error_message: The raw error message to categorize. Can be None
                for unknown errors.

        Returns:
            Tuple containing:
            - ErrorCategory: The classified error category
            - str: User-friendly error message for display
            - list[str]: List of actionable suggestions for error resolution

        Note:
            Pattern matching is case-insensitive and uses substring matching
            for maximum flexibility. If no patterns match, the error is
            classified as UNKNOWN with generic suggestions.

        Example:
            >>> category, msg, suggestions = categorize_error("corrupted file")
            >>> print(category)  # ErrorCategory.FILE_QUALITY
            >>> print(len(suggestions))  # 5 specific suggestions
        """
        if not error_message:
            return (
                ErrorCategory.UNKNOWN,
                "An unknown error occurred during processing.",
                ["Try uploading the file again", "Contact support if the problem persists"],
            )

        # Security: Limit error message length to prevent memory issues
        if len(error_message) > 10000:
            logger.warning(f"Error message truncated from {len(error_message)} to 10000 characters")
            error_message = error_message[:10000]

        error_lower = error_message.lower()

        # Check for file quality issues
        if any(
            pattern in error_lower for pattern in ErrorCategorizationService.FILE_QUALITY_PATTERNS
        ):
            return ErrorCategorizationService._handle_file_quality_error(error_message)

        # Check for speech detection issues
        if any(pattern in error_lower for pattern in ErrorCategorizationService.NO_SPEECH_PATTERNS):
            return ErrorCategorizationService._handle_no_speech_error(error_message)

        # Check for format issues
        if any(pattern in error_lower for pattern in ErrorCategorizationService.FORMAT_PATTERNS):
            return ErrorCategorizationService._handle_format_error(error_message)

        # Check for network issues
        if any(pattern in error_lower for pattern in ErrorCategorizationService.NETWORK_PATTERNS):
            return ErrorCategorizationService._handle_network_error(error_message)

        # Check for permission issues
        if any(
            pattern in error_lower for pattern in ErrorCategorizationService.PERMISSION_PATTERNS
        ):
            return ErrorCategorizationService._handle_permission_error(error_message)

        # Generic processing error
        return ErrorCategorizationService._handle_generic_error(error_message)

    @staticmethod
    def _handle_file_quality_error(error_message: str) -> tuple[ErrorCategory, str, list[str]]:
        """Handle file quality/corruption errors."""
        return (
            ErrorCategory.FILE_QUALITY,
            f"File Quality Issue: {error_message}",
            [
                "Check if the file plays correctly on your device",
                "Try converting to MP3, WAV, or MP4 format",
                "Ensure the file isn't password protected or DRM-locked",
                "Consider re-recording if the original source is problematic",
                "Verify the file wasn't corrupted during download or transfer",
            ],
        )

    @staticmethod
    def _handle_no_speech_error(error_message: str) -> tuple[ErrorCategory, str, list[str]]:
        """Handle no speech detected errors."""
        return (
            ErrorCategory.NO_SPEECH,
            f"No Speech Detected: {error_message}",
            [
                "Ensure the file contains clear, audible speech",
                "Check if speech is too quiet or unclear",
                "Reduce background noise if possible",
                "Verify this isn't a music-only or instrumental file",
                "Try uploading a different section with clearer audio",
            ],
        )

    @staticmethod
    def _handle_format_error(error_message: str) -> tuple[ErrorCategory, str, list[str]]:
        """Handle format/encoding errors."""
        return (
            ErrorCategory.FORMAT_ISSUE,
            f"Format Issue: {error_message}",
            [
                "Convert to a supported format (MP3, WAV, MP4, M4A)",
                "Try re-encoding with standard settings",
                "Check if the file uses an uncommon codec",
                "Ensure the file extension matches the actual format",
                "Use a different audio/video converter tool",
            ],
        )

    @staticmethod
    def _handle_network_error(error_message: str) -> tuple[ErrorCategory, str, list[str]]:
        """Handle network/download errors."""
        return (
            ErrorCategory.NETWORK_ERROR,
            f"Network Issue: {error_message}",
            [
                "Check your internet connection",
                "Verify the URL is accessible and not expired",
                "Try the upload again in a few minutes",
                "Download the file locally first, then upload",
                "Contact the content provider if URL access issues persist",
            ],
        )

    @staticmethod
    def _handle_permission_error(error_message: str) -> tuple[ErrorCategory, str, list[str]]:
        """Handle permission/access errors."""
        return (
            ErrorCategory.PERMISSION_ERROR,
            f"Access Issue: {error_message}",
            [
                "Ensure you have permission to access this content",
                "Check if the content is behind a paywall or login",
                "Verify the content isn't DRM-protected",
                "Try downloading the file manually first",
                "Contact the content owner for access permissions",
            ],
        )

    @staticmethod
    def _handle_generic_error(error_message: str) -> tuple[ErrorCategory, str, list[str]]:
        """Handle generic processing errors."""
        return (
            ErrorCategory.PROCESSING_ERROR,
            f"Processing Failed: {error_message}",
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
            - category: String representation of the error category
            - user_message: User-friendly error description
            - suggestions: List of actionable resolution steps
            - original_error: The original error message (for debugging)
            - is_retryable: Boolean indicating if the error is worth retrying

        Note:
            The is_retryable field helps frontends determine whether to show
            retry buttons or encourage users to fix the underlying issue first.

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
            "original_error": error_message,
            "is_retryable": category
            in [ErrorCategory.NETWORK_ERROR, ErrorCategory.PROCESSING_ERROR, ErrorCategory.UNKNOWN],
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
