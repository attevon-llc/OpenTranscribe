"""Size ceiling for a persisted chat message's ``msg_metadata`` JSON blob.

``ChatMessage.msg_metadata`` (``app/models/chat.py``) is an unbounded JSONB
column. It accumulates diagnostics for one turn — retrieval counts, the
overview reducer's stats and (Wave 2) a query plan, a per-speaker resolution
result, and a list of failed retrieval "legs". None of those are bounded at
the source, and unlike everything else on the message row there is no
schema-level cap on a JSONB column: a plan over a pathological scope, or a
resolution dict carrying one entry per candidate speaker, can grow without
limit. :func:`cap_msg_metadata` is the one place that enforces a ceiling.

**Caller.** ``services/chat/service.py``, immediately before the metadata
dict is attached to the message row and committed — not wired in from this
module (that file is out of scope here); this is the utility the caller is
expected to reach for rather than inventing a second cap.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Ceiling on a persisted message's `msg_metadata` JSON, in bytes. Generous
#: for today's scalar diagnostics (a handful of counts, flags, and a
#: rewritten query rarely exceed a few hundred bytes) and small next to a
#: message body — chosen so an ordinary turn never hits it and only a
#: pathological container-valued field (a huge plan, an oversized speaker
#: resolution) does.
DEFAULT_MAX_METADATA_BYTES = 8_000

#: Added to a capped dict so a capped blob is distinguishable from a small
#: one, rather than a diagnostics panel silently rendering less than it
#: should with no signal that anything was removed.
CAPPED_MARKER_KEY = "metadata_capped"
#: Names of the keys dropped to fit, in the order they were dropped
#: (largest/most-recently-dropped last). Recorded so the loss is visible.
DROPPED_KEYS_KEY = "metadata_capped_keys"

_SCALAR_TYPES = (str, int, float, bool, type(None))


def _json_size(value: Any) -> int:
    """Byte size of ``value`` if serialized alone, or ``0`` if it cannot be.

    ``default=str`` rather than raising: a metadata value that is not
    natively JSON-safe (an enum, a dataclass a future caller forgot to
    convert) must not crash the cap — it degrades to "treat it as expensive"
    via a large stringified size, never to an exception reaching persistence.
    """
    try:
        return len(json.dumps(value, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def cap_msg_metadata(
    metadata: dict[str, Any] | None,
    *,
    max_bytes: int = DEFAULT_MAX_METADATA_BYTES,
) -> dict[str, Any] | None:
    """Bound the size of a persisted message's ``msg_metadata`` JSON blob.

    Under budget, returns ``metadata`` unchanged (same object — no copy for
    the common case, which is every ordinary turn). Over budget, keeps every
    SCALAR-valued key unconditionally (they are what the UI mostly renders —
    counts, flags, the rewritten query — and are cheap even in bulk), then
    adds CONTAINER-valued keys (list/dict) back in smallest-first, so the one
    thing actually responsible for the overrun — typically a single
    oversized field, not a uniform bloat across many — is the one dropped,
    and every other diagnostic survives intact.

    Keys are dropped WHOLE, never partially truncated: a half-rendered plan
    or a speaker-resolution list missing entries with no indication reads as
    a data-integrity bug, not a budget decision. If even the scalar keys
    alone exceed ``max_bytes`` (a pathologically large STRING scalar, not
    covered by the container logic above), they are still returned as-is —
    this function bounds container growth, not individual scalar length; a
    caller producing an unbounded string value needs its own cap.

    Args:
        metadata: The dict about to be attached to a message row, or
            ``None``/falsy (returned unchanged either way).
        max_bytes: The ceiling, measured as UTF-8 JSON bytes.

    Returns:
        ``metadata`` unchanged if already within budget, else a new dict
        containing every scalar key, as many container keys as fit, and
        `CAPPED_MARKER_KEY` / `DROPPED_KEYS_KEY` describing what was cut.
    """
    if not metadata:
        return metadata

    total = _json_size(metadata)
    if total <= max_bytes:
        return metadata

    scalars = {k: v for k, v in metadata.items() if isinstance(v, _SCALAR_TYPES)}
    containers = {k: v for k, v in metadata.items() if not isinstance(v, _SCALAR_TYPES)}
    smallest_first = sorted(containers.items(), key=lambda kv: _json_size(kv[1]))

    kept: dict[str, Any] = dict(scalars)
    dropped: list[str] = []
    for key, value in smallest_first:
        candidate = {**kept, key: value}
        if _json_size(candidate) <= max_bytes:
            kept = candidate
        else:
            dropped.append(key)

    if not dropped:
        # Every container fit once ordered smallest-first; nothing was cut.
        return kept

    logger.info(
        "Capped msg_metadata at %d bytes (was %d): dropped %s",
        max_bytes,
        total,
        dropped,
    )
    kept[CAPPED_MARKER_KEY] = True
    kept[DROPPED_KEYS_KEY] = dropped
    return kept
