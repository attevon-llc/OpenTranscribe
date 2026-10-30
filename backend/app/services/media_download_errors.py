"""User-facing error guidance for failed media URL downloads (auth, geo, platform hints)."""

# Authentication and access error patterns with user-friendly messages
AUTH_ERROR_PATTERNS = {
    "logged-in": "requires a logged-in account",
    "log in": "requires login",
    "sign in": "requires sign-in",
    "credentials": "requires authentication credentials",
    "cookies": "requires browser cookies for authentication",
    "private": "is private or restricted",
    "age": "is age-restricted and requires verification",
    "geo": "is not available in your region (geo-restricted)",
    "removed": "has been removed or is unavailable",
    "unavailable": "is currently unavailable",
    "blocked": "is blocked or restricted",
    "premium": "requires a premium subscription",
    "members only": "is members-only content",
    "subscriber": "requires a subscription",
}

# Platform-specific guidance messages
PLATFORM_GUIDANCE = {
    "vimeo": "Most Vimeo videos require authentication. Try YouTube or Dailymotion instead.",
    "instagram": "Instagram videos typically require login. Try a different platform.",
    "facebook": "Facebook videos often require authentication.",
    "twitter": "Some Twitter/X videos may require login to access.",
    "x": "Some X (Twitter) videos may require login to access.",
    "tiktok": "Some TikTok videos may be region-restricted or require authentication.",
    "linkedin": "LinkedIn videos require authentication.",
    "patreon": "Patreon videos require a subscription to access.",
    "onlyfans": "OnlyFans content requires a subscription.",
    "twitch": "Some Twitch VODs may be subscriber-only.",
}

# Recommended platforms for public video downloads (YouTube is most reliable)
RECOMMENDED_PLATFORMS = ["YouTube", "Dailymotion", "Twitter/X"]


def _detect_auth_error(error_message: str) -> tuple[bool, str]:
    """
    Detect if an error message indicates an authentication-related issue.

    Args:
        error_message: The error message from yt-dlp

    Returns:
        Tuple of (is_auth_error, matched_reason)
    """
    error_lower = error_message.lower()
    for pattern, reason in AUTH_ERROR_PATTERNS.items():
        if pattern in error_lower:
            return True, reason
    return False, ""


def _get_platform_from_error(error_message: str) -> str:
    """
    Try to extract platform name from error message or URL in the error.

    Args:
        error_message: The error message from yt-dlp

    Returns:
        Platform name or empty string
    """
    error_lower = error_message.lower()

    # Check for known platform names in the error
    platforms = [
        "vimeo",
        "instagram",
        "facebook",
        "twitter",
        "x.com",
        "tiktok",
        "linkedin",
        "patreon",
        "twitch",
        "youtube",
    ]
    for platform in platforms:
        if platform in error_lower:
            # Normalize x.com to twitter
            return "twitter" if platform == "x.com" else platform
    return ""


def create_user_friendly_error(error_message: str, url: str = "") -> str:
    """
    Create a user-friendly error message from a yt-dlp error.

    Detects authentication-related errors and provides helpful guidance
    about platform limitations.

    Args:
        error_message: The raw error message from yt-dlp
        url: The original URL (optional, for platform detection)

    Returns:
        User-friendly error message with guidance
    """
    is_auth_error, auth_reason = _detect_auth_error(error_message)

    # Try to detect platform from error message or URL
    platform = _get_platform_from_error(error_message)
    if not platform and url:
        platform = _get_platform_from_error(url)

    if is_auth_error:
        # Build user-friendly message
        if platform:
            platform_title = platform.title() if platform != "x" else "X (Twitter)"
            guidance = PLATFORM_GUIDANCE.get(platform.lower(), "")

            if guidance:
                return (
                    f"This {platform_title} video {auth_reason}. {guidance} "
                    f"For best results, try {', '.join(RECOMMENDED_PLATFORMS)}."
                )
            return (
                f"This {platform_title} video {auth_reason}. "
                f"For best results, try publicly accessible videos on "
                f"{', '.join(RECOMMENDED_PLATFORMS)}."
            )

        # Generic auth error without platform
        return (
            f"This video {auth_reason}. Some platforms restrict video downloads "
            f"to authenticated users. For best results, try publicly accessible "
            f"videos on {', '.join(RECOMMENDED_PLATFORMS)}."
        )

    # Not an auth error - return the original message but cleaned up
    # Remove common yt-dlp prefixes
    cleaned = error_message
    prefixes_to_remove = [
        "ERROR: ",
        "DownloadError: ",
        "[download] ",
        "[generic] ",
    ]
    for prefix in prefixes_to_remove:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]

    return cleaned
