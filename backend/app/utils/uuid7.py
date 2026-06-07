"""Dependency-free RFC 9562 UUID version 7 generator.

UUIDv7 (RFC 9562, https://www.rfc-editor.org/rfc/rfc9562) is a time-ordered UUID
whose leading 48 bits encode a big-endian Unix timestamp in milliseconds. Because
new values sort (lexically and as PostgreSQL ``uuid``) in creation order, they give
far better B-tree index locality and insert performance than random UUIDv4 — new
rows append to the right edge of the index instead of scattering across it.

Layout (128 bits, RFC 9562 §5.7)::

    0                   1                   2                   3
    0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |                           unix_ts_ms                          |  48 bits
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |  ver  |         rand_a        |var|          rand_b           |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |                           rand_b                              |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

  * bits  0-47  : unix_ts_ms (big-endian milliseconds since the Unix epoch)
  * bits 48-51  : version = 0b0111 (7)
  * bits 52-63  : rand_a — used here as a 12-bit intra-millisecond monotonic counter
  * bits 64-65  : variant = 0b10
  * bits 66-127 : rand_b — 62 random bits

In-repo (no PyPI dependency) deliberately: the implementation is ~20 lines, has no
supply-chain surface, and returns a standard ``uuid.UUID`` so every existing call
site (``str(x.uuid)``, MinIO keys, WebSocket payloads, Pydantic serialization) is
unchanged.

Monotonicity: within a single millisecond the 12-bit ``rand_a`` field is used as a
counter that increments on each call, guaranteeing that a burst of UUIDs minted in
the same millisecond still sorts in creation order. The counter resets when the
millisecond advances. If the counter saturates (>4096 calls in one ms) the
millisecond is nudged forward by one so ordering is preserved. ``rand_b`` stays
fully random for uniqueness.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

__all__ = ["uuid7"]

_lock = threading.Lock()
_last_ms = -1
_counter = 0
_COUNTER_MAX = 0xFFF  # 12 bits of rand_a used as the monotonic counter


def uuid7() -> uuid.UUID:
    """Return a new RFC 9562 version 7 UUID (time-ordered).

    Thread-safe. Monotonic within and across milliseconds so a burst of values
    minted close together still sorts in creation order.

    Returns:
        uuid.UUID: a UUID with version field 7 and the RFC 4122 variant (0b10).
    """
    global _last_ms, _counter

    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            _last_ms = ms
            _counter = 0
        else:
            # Same millisecond (or a clock that went backwards): advance the
            # intra-ms counter to keep values strictly increasing.
            _counter += 1
            if _counter > _COUNTER_MAX:
                # Counter exhausted: borrow from the next millisecond.
                _last_ms += 1
                _counter = 0
            ms = _last_ms
        counter = _counter

    # 48-bit big-endian timestamp.
    ts = ms & 0xFFFFFFFFFFFF
    # 62 random bits for rand_b.
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)

    value = ts << 80
    value |= 0x7 << 76  # version = 7
    value |= (counter & 0xFFF) << 64  # rand_a = monotonic counter
    value |= 0b10 << 62  # variant = 0b10
    value |= rand_b

    return uuid.UUID(int=value)
