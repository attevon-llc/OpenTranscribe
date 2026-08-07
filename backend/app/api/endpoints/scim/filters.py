"""The narrow slice of SCIM filtering and pagination this server supports.

RFC 7644 §3.4.2.2 defines a whole filter grammar — ``and``/``or``/``not``, grouping,
``co``/``sw``/``pr``/``gt``, complex attribute paths. Implementing a fraction of it
and silently ignoring the rest is worse than not implementing it: a client that asks
for ``userName eq "x" and active eq true`` and gets back every user will happily act
on the answer.

So this module supports exactly one production, refuses everything else with
``scimType=invalidFilter``, and says so in ``ServiceProviderConfig``:

    <attribute> eq "<value>"

with ``<attribute>`` ∈ ``userName`` / ``externalId`` for Users and ``displayName`` /
``externalId`` for Groups. That is the shape Okta and Entra actually send when they
reconcile a single resource, which is the only filter either of them needs.
"""

from __future__ import annotations

import re

from app.api.endpoints.scim.errors import SCIM_TYPE_INVALID_FILTER
from app.api.endpoints.scim.errors import SCIMError
from app.api.endpoints.scim.errors import bad_request
from app.schemas.scim import DEFAULT_PAGE_SIZE
from app.schemas.scim import MAX_PAGE_SIZE

#: ``attr eq "value"``, case-insensitive on the operator, tolerant of extra spaces.
#: Single quotes are accepted too — not RFC-legal, but several connectors emit them
#: and rejecting is a support ticket rather than a security property.
_EQ_FILTER = re.compile(r'^\s*(?P<attr>[A-Za-z][\w.]*)\s+eq\s+["\'](?P<value>[^"\']*)["\']\s*$')


def parse_eq_filter(raw: str | None, *, allowed: tuple[str, ...]) -> tuple[str, str] | None:
    """Parse the one supported filter production.

    Args:
        raw: The ``filter`` query parameter, or ``None``.
        allowed: Attribute names this collection can filter on, in SCIM spelling.

    Returns:
        ``(attribute, value)`` with the attribute normalised to the spelling in
        *allowed*, or ``None`` when no filter was supplied.

    Raises:
        SCIMError: 400 ``invalidFilter`` for anything outside the supported
            production, including a supported operator on an unsupported attribute.
            Refusing is the point — see the module docstring.
    """
    if raw is None or not raw.strip():
        return None

    match = _EQ_FILTER.match(raw)
    if not match:
        raise SCIMError(
            400,
            (
                f"Unsupported filter {raw!r}. This server supports only "
                f"'<attribute> eq \"<value>\"' on: {', '.join(allowed)}."
            ),
            scim_type=SCIM_TYPE_INVALID_FILTER,
        )

    attr = match.group("attr")
    canonical = next((a for a in allowed if a.lower() == attr.lower()), None)
    if canonical is None:
        raise SCIMError(
            400,
            f"Filtering on {attr!r} is not supported. Supported: {', '.join(allowed)}.",
            scim_type=SCIM_TYPE_INVALID_FILTER,
        )
    return canonical, match.group("value")


def parse_pagination(start_index: int, count: int | None) -> tuple[int, int]:
    """Turn SCIM's 1-based paging parameters into an SQL offset/limit.

    Args:
        start_index: SCIM ``startIndex``, 1-based. Values below 1 are clamped to 1,
            which RFC 7644 §3.4.2.4 requires rather than treating as an error.
        count: SCIM ``count``. ``None`` uses the default page size; the value is
            capped at :data:`~app.schemas.scim.MAX_PAGE_SIZE` so a client cannot ask
            for the whole table in one query.

    Returns:
        ``(offset, limit)``.

    Raises:
        SCIMError: 400 for a negative ``count``.
    """
    if count is not None and count < 0:
        raise bad_request("count must not be negative")
    start = max(1, start_index)
    limit = DEFAULT_PAGE_SIZE if count is None else min(count, MAX_PAGE_SIZE)
    return start - 1, limit
