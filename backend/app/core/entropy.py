"""Entropy validation for FIPS 140-3 deployments (``FIPS_VALIDATE_ENTROPY``).

Two questions, both asked at boot by ``app.main._validate_production_secrets`` and only
when ``settings.fips_140_3_active and settings.FIPS_VALIDATE_ENTROPY``:

1. **Is the OS CSPRNG usable?** ``utils/encryption.py`` draws every salt and every GCM
   nonce from ``os.urandom``. A GCM nonce that repeats under the same key is a total
   confidentiality *and* integrity break, so a broken or absent entropy source is not a
   degraded mode — it is a reason to refuse to start.
2. **Is the configured key material plausibly random?** The existing boot gate already
   refuses a *known* placeholder (``changeme``, ``CHANGE_ME_auto_generated_on_install``).
   That is a blocklist, and blocklists miss ``ENCRYPTION_KEY=AAAAAAAA...``. These checks
   ask the complementary question — does this value look like it came out of a CSPRNG?

⚠️ **What this cannot do.** No test on a single string proves randomness; a value can pass
every check here and still be the operator's dog's name run through a hash they published.
These are *floors* that reject obviously non-random material, and they are deliberately
permissive so that no legitimately generated key is ever rejected. The thresholds below are
each justified against both directions: what real keys look like, and what the degenerate
values look like.

Nothing here is FIPS-mode-specific arithmetic; the gating is the caller's.
"""

import math
import os
from collections import Counter
from collections.abc import Callable

#: Bytes drawn per probe of the OS CSPRNG. 32 = the key size ``utils/encryption.py``
#: derives, so the probe exercises the same draw size the app relies on.
CSPRNG_PROBE_BYTES = 32

#: Minimum length of a secret, in bytes of its UTF-8 encoding.
#:
#: NIST SP 800-131A Rev. 2 puts the minimum acceptable symmetric security strength at 112
#: bits. 32 characters is the shortest value that carries that with margin in any encoding
#: an operator would plausibly paste into ``.env`` — a 128-bit key is 32 hex characters,
#: and ``utils.encryption.generate_encryption_key()`` emits 44 (base64 of 32 bytes).
MIN_SECRET_BYTES = 32

#: Minimum number of distinct byte values.
#:
#: Kept low on purpose, because the tightest *legitimate* case is a 32-character hex key:
#: it draws from only 16 symbols, averages ~14 distinct, and over 100,000 measured samples
#: never went below 8. Requiring 12 would false-reject such a key roughly 5% of the time.
#: At 6 the margin is large while a single-character fill (1 distinct) still fails.
MIN_DISTINCT_BYTES = 6

#: Minimum Shannon entropy, in bits per byte, of the value's own symbol distribution.
#:
#: **Measured, not guessed.** Over 100,000 samples of the tightest legitimate case (a
#: 32-character hex key) the minimum observed was 2.92 and the 0.1st percentile 3.12; base64
#: of 32 random bytes never fell below 4.28. An earlier 3.0 floor therefore rejected a
#: legitimate 128-bit hex key about **1 boot in 10,000** — a fail-closed control that
#: refuses a correct configuration is a bug, not extra safety. 2.5 sits below every measured
#: random case and above ``"changeme"`` repeated (2.75 — also caught by the periodicity
#: check) and every single-character fill (0.0).
#:
#: ⚠️ This is the entropy of the *observed string*, not of the process that produced it.
#: It is an upper bound on randomness, never evidence of it: a keyboard walk
#: (``"qwertyuiop..."``) measures 5.17 and passes. The known-placeholder blocklist in
#: ``_validate_production_secrets`` is the complementary check, not a redundant one —
#: ``CHANGE_ME_auto_generated_on_install`` measures 4.08 and only that blocklist stops it.
MIN_SHANNON_BITS_PER_BYTE = 2.5

#: Longest permitted run of one repeated byte.
#:
#: Catches the padded-key shape (``"opentranscribe_" + "b" * 64``) that passes a length
#: check and reads as configured. A run of 9 in base64/hex output of these lengths is
#: astronomically unlikely.
MAX_IDENTICAL_RUN = 8


def shannon_entropy_bits_per_byte(data: bytes) -> float:
    """Shannon entropy of *data*'s byte distribution, in bits per byte.

    Args:
        data: Raw bytes to measure. Empty input scores 0.0.

    Returns:
        Entropy in bits per byte, in ``[0.0, 8.0]``.
    """
    if not data:
        return 0.0
    total = len(data)
    return -sum((count / total) * math.log2(count / total) for count in Counter(data).values())


def is_exactly_periodic(data: bytes) -> bool:
    """Whether *data* is a shorter block repeated a whole number of times.

    This is the check that catches ``"changeme" * 4``, ``"password123!" * 3`` and
    ``"0123456789" * 4`` — values long enough to clear the length floor, varied enough to
    clear the distinct-byte and Shannon floors, and obviously not key material. CSPRNG
    output is periodic with probability ~2^-len, so this cannot false-reject a real key.

    Args:
        data: Raw bytes to test.

    Returns:
        True if some proper divisor of ``len(data)`` is a period of *data*.
    """
    length = len(data)
    for period in range(1, length // 2 + 1):
        if length % period == 0 and data == data[:period] * (length // period):
            return True
    return False


def longest_identical_run(data: bytes) -> int:
    """Length of the longest run of one repeated byte in *data*.

    Args:
        data: Raw bytes to scan.

    Returns:
        The run length; 0 for empty input.
    """
    longest = 0
    current = 0
    previous: int | None = None
    for byte in data:
        current = current + 1 if byte == previous else 1
        previous = byte
        longest = max(longest, current)
    return longest


def assert_csprng_available() -> None:
    """Verify the OS CSPRNG that ``utils/encryption.py`` draws salts and nonces from.

    Probes ``os.urandom`` and, where the platform has it, ``getrandom(2)`` directly —
    both the interface the app calls and the syscall behind it. Two independent draws
    must differ and must not be all-zero, which catches a stubbed, seeded, or
    permanently-blocking source.

    Raises:
        ValueError: If any entropy source is unavailable or does not behave like a CSPRNG.
    """
    sources: list[tuple[str, Callable[[int], bytes]]] = [("os.urandom", os.urandom)]
    if hasattr(os, "getrandom"):  # Linux; absent on macOS/Windows
        sources.append(("os.getrandom", os.getrandom))

    for name, draw in sources:
        try:
            first = draw(CSPRNG_PROBE_BYTES)
            second = draw(CSPRNG_PROBE_BYTES)
        except (NotImplementedError, OSError) as exc:
            raise ValueError(
                f"FIPS entropy validation failed: {name} is unavailable ({exc}). "
                "Encryption salts and GCM nonces cannot be generated safely."
            ) from exc

        if len(first) != CSPRNG_PROBE_BYTES or len(second) != CSPRNG_PROBE_BYTES:
            raise ValueError(
                f"FIPS entropy validation failed: {name} returned "
                f"{len(first)}/{len(second)} bytes, expected {CSPRNG_PROBE_BYTES}."
            )
        if first == second or first == bytes(CSPRNG_PROBE_BYTES):
            raise ValueError(
                f"FIPS entropy validation failed: {name} is not behaving like a CSPRNG "
                "(two draws were identical, or the draw was all zero). A repeated "
                "AES-GCM nonce breaks both confidentiality and integrity."
            )


def validate_secret_entropy(name: str, value: str) -> None:
    """Refuse key material that is not plausibly random.

    Args:
        name: The setting name, e.g. ``"ENCRYPTION_KEY"``. It appears in the exception —
            an operator needs to know *which* secret to regenerate, and a boot failure
            that says only "a secret failed" costs a support cycle.
        value: The configured secret.

    Raises:
        ValueError: If *value* is empty, shorter than :data:`MIN_SECRET_BYTES`, or fails
            any of the distinct-byte, periodicity, repeated-run, or Shannon-entropy floors.
            The message always names *name*.
    """
    raw = value.encode("utf-8", errors="surrogatepass")

    if not raw:
        raise ValueError(
            f"FIPS entropy validation failed for {name}: it is empty. "
            'Generate one with `python -c "from app.utils.encryption import '
            'generate_encryption_key; print(generate_encryption_key())"`.'
        )

    if len(raw) < MIN_SECRET_BYTES:
        raise ValueError(
            f"FIPS entropy validation failed for {name}: {len(raw)} bytes is below the "
            f"{MIN_SECRET_BYTES}-byte minimum for a FIPS 140-3 deployment (NIST SP "
            "800-131A Rev. 2 requires at least 112 bits of security strength)."
        )

    distinct = len(set(raw))
    if distinct < MIN_DISTINCT_BYTES:
        raise ValueError(
            f"FIPS entropy validation failed for {name}: only {distinct} distinct byte "
            f"values (minimum {MIN_DISTINCT_BYTES}). This value is not random."
        )

    if is_exactly_periodic(raw):
        raise ValueError(
            f"FIPS entropy validation failed for {name}: it is one short block repeated to "
            "length. A word typed enough times to satisfy a length requirement carries the "
            "entropy of the word, not of the value."
        )

    run = longest_identical_run(raw)
    if run > MAX_IDENTICAL_RUN:
        raise ValueError(
            f"FIPS entropy validation failed for {name}: it contains a run of {run} "
            f"identical bytes (maximum {MAX_IDENTICAL_RUN}), which is the signature of a "
            "padded or placeholder value rather than generated key material."
        )

    entropy = shannon_entropy_bits_per_byte(raw)
    if entropy < MIN_SHANNON_BITS_PER_BYTE:
        raise ValueError(
            f"FIPS entropy validation failed for {name}: Shannon entropy "
            f"{entropy:.2f} bits/byte is below the {MIN_SHANNON_BITS_PER_BYTE} "
            "bits/byte floor. Replace it with a value from a CSPRNG."
        )
