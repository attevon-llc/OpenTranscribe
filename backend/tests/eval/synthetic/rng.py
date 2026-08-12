"""SplitMix64 — the corpus generator's only source of randomness.

Deliberately **not** :mod:`random`. Byte-identical regeneration is a published claim of
the synthetic tier, and CPython's ``random`` is only guaranteed stable for the raw
Mersenne Twister stream — the *helpers* built on it are not. ``random.shuffle`` dropped
its ``random`` argument in 3.11 and ``random.sample`` has changed its set/sequence
handling more than once; either would silently produce a different corpus from the same
seed on a different interpreter. SplitMix64 is nine lines of arithmetic frozen in this
file, so "seed 20260812 reproduces the corpus" holds on any Python that can multiply.

Reference: Steele, Lea & Flood, *Fast Splittable Pseudorandom Number Generators*,
OOPSLA 2014 (the ``SplittableRandom`` mixing function).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")

_MASK64 = (1 << 64) - 1
_GAMMA = 0x9E3779B97F4A7C15
_MIX_A = 0xBF58476D1CE4E5B9
_MIX_B = 0x94D049BB133111EB


def derive_seed(*parts: object) -> int:
    """Derive a 64-bit sub-seed from a stable string join of ``parts``.

    Hash-derived rather than sequential so a meeting's content depends only on its own
    key, never on how many meetings were generated before it. That is what lets a single
    meeting be regenerated, or the corpus be generated in any order, and still match.

    Args:
        *parts: Values joined with ``":"`` after ``str()``.

    Returns:
        A 64-bit unsigned integer suitable for :class:`Rng`.
    """
    joined = ":".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(joined, digest_size=8).digest(), "big")


class Rng:
    """A deterministic, self-contained PRNG with the helpers the generator needs."""

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        """Initialise the stream.

        Args:
            seed: Any integer; only the low 64 bits are used.
        """
        self._state = seed & _MASK64

    def next_u64(self) -> int:
        """Return the next 64-bit unsigned value and advance the stream."""
        self._state = (self._state + _GAMMA) & _MASK64
        z = self._state
        z = ((z ^ (z >> 30)) * _MIX_A) & _MASK64
        z = ((z ^ (z >> 27)) * _MIX_B) & _MASK64
        return z ^ (z >> 31)

    def random(self) -> float:
        """Return a float in ``[0.0, 1.0)`` with 53 bits of resolution."""
        return (self.next_u64() >> 11) / (1 << 53)

    def randint(self, low: int, high: int) -> int:
        """Return an integer in ``[low, high]`` (both inclusive)."""
        if high < low:
            raise ValueError(f"empty range: [{low}, {high}]")
        return low + self.next_u64() % (high - low + 1)

    def chance(self, probability: float) -> bool:
        """Return True with the given probability."""
        return self.random() < probability

    def choice(self, seq: Sequence[T]) -> T:
        """Return one uniformly chosen element of ``seq``."""
        if not seq:
            raise ValueError("choice from an empty sequence")
        return seq[self.next_u64() % len(seq)]

    def shuffled(self, seq: Sequence[T]) -> list[T]:
        """Return a Fisher-Yates shuffled copy of ``seq``."""
        out = list(seq)
        for i in range(len(out) - 1, 0, -1):
            j = self.next_u64() % (i + 1)
            out[i], out[j] = out[j], out[i]
        return out

    def sample(self, seq: Sequence[T], k: int) -> list[T]:
        """Return ``k`` distinct elements of ``seq``, order randomised."""
        if k > len(seq):
            raise ValueError(f"cannot sample {k} from {len(seq)}")
        return self.shuffled(seq)[:k]

    def gauss(self) -> float:
        """Return a standard-normal variate (Box-Muller, no cached second draw).

        The second Box-Muller variate is discarded on purpose: caching it would make a
        draw depend on whether the *previous* call happened, which couples otherwise
        independent generation sites and breaks single-meeting regeneration.
        """
        u1 = max(self.random(), 1e-12)
        u2 = self.random()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def lognormal(self, median: float, sigma: float) -> float:
        """Return a lognormal variate with the given median and log-scale sigma."""
        return median * math.exp(sigma * self.gauss())

    def clamped_lognormal(self, median: float, sigma: float, low: float, high: float) -> float:
        """Return :meth:`lognormal` clamped into ``[low, high]``."""
        return min(max(self.lognormal(median, sigma), low), high)
