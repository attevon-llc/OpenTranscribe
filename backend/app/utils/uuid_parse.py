"""The single UUID parse core shared by callers that differ only in what a bad value means."""

import uuid as uuid_pkg


def parse_uuid_or_none(value: object) -> uuid_pkg.UUID | None:
    """Parse ``value`` to a ``UUID``; ``None`` for anything that is not one.

    Accepts a ``UUID`` (returned as is) or anything whose ``str()`` is a UUID in the
    spellings ``uuid.UUID`` takes (dashed, upper-case, braced, ``urn:uuid:``, bare hex).
    Never raises. What a ``None`` result means (skip, 404, 400) is the caller's policy.
    """
    if isinstance(value, uuid_pkg.UUID):
        return value
    try:
        return uuid_pkg.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
