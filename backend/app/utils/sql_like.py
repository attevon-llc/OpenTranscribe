"""SQL LIKE-pattern escaping."""


def escape_like(value: str) -> str:
    """Escape LIKE/ILIKE metacharacters so ``value`` matches literally.

    Pairs with ``escape="\\\\"`` on the comparison (``col.ilike(pat, escape="\\\\")``).
    The backslash is escaped first, otherwise a user-supplied ``\\`` would escape the
    next metacharacter or the closing ``%`` of the pattern.

    Args:
        value: Raw user text.

    Returns:
        The text with ``\\``, ``%`` and ``_`` backslash-escaped.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
