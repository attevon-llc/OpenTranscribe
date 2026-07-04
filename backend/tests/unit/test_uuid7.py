"""Unit tests for the dependency-free RFC 9562 UUIDv7 generator."""

from __future__ import annotations

import uuid

from app.utils.uuid7 import uuid7


def test_returns_uuid_instance():
    """uuid7() returns a real uuid.UUID (so str()/serialization is unchanged)."""
    value = uuid7()
    assert isinstance(value, uuid.UUID)


def test_version_field_is_7():
    """The 4-bit version field must be 7 per RFC 9562 §5.7."""
    for _ in range(1000):
        assert uuid7().version == 7


def test_variant_is_rfc_4122():
    """The variant must be the RFC 4122 variant (0b10xx)."""
    for _ in range(1000):
        value = uuid7()
        # uuid.UUID.variant returns RFC_4122 for the 0b10 variant bits.
        assert value.variant == uuid.RFC_4122
        # Explicit bit check: top two bits of the clock_seq_hi byte are 0b10.
        variant_byte = value.bytes[8]
        assert (variant_byte & 0b1100_0000) == 0b1000_0000


def test_timestamp_is_recent_and_big_endian():
    """The leading 48 bits encode the current Unix time in milliseconds."""
    import time

    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000

    ts_ms = int.from_bytes(value.bytes[:6], "big")
    assert before <= ts_ms <= after


def test_monotonic_ordering_of_a_burst():
    """A burst minted back-to-back must sort in creation order."""
    burst = [uuid7() for _ in range(10_000)]
    # Strictly increasing — same-ms values are ordered by the rand_a counter.
    for prev, cur in zip(burst, burst[1:], strict=False):
        assert prev < cur, f"ordering violated: {prev} !< {cur}"
    assert burst == sorted(burst)


def test_uniqueness():
    """No collisions across a large batch."""
    batch = {uuid7() for _ in range(50_000)}
    assert len(batch) == 50_000


def test_round_trip_parse():
    """Values round-trip through their canonical string form unchanged."""
    for _ in range(1000):
        value = uuid7()
        assert uuid.UUID(str(value)) == value
        # Canonical hyphenated 36-char form — identical to today's uuid4 output.
        assert len(str(value)) == 36
        assert str(value).count("-") == 4
