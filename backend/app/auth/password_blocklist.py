"""Common / breached-password screening (NIST SP 800-63B-4 section 3.1.1.2).

Two independent checks, both fed the *NFKC-normalised* password so a full-width or
ligature spelling of a listed password does not slip past:

* an **offline** blocklist - a file shipped in the repo (``data/common_passwords.txt``,
  see ``data/README.md`` for source and licence) or an operator-supplied file via
  ``PASSWORD_BLOCKLIST_PATH``. Works on air-gapped installs and never leaves the host.
* an optional **online k-anonymity range lookup** against a Have I Been Pwned-style
  API (``PASSWORD_HIBP_ENABLED``, default off). Only the first five hex characters of
  the password's SHA-1 are sent; the full hash and the password never leave the host.

The online lookup **fails open**: a timeout, DNS failure, non-200 or malformed reply
logs a WARNING and the password is accepted (provided it passed every other check).
That is deliberate - an outage of a third-party service must not stop users
registering or recovering their account - and the cost is that a password is
only screened when the service answers, so the offline list remains the baseline.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import unicodedata
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

BUNDLED_BLOCKLIST_PATH = Path(__file__).parent / "data" / "common_passwords.txt"

#: Context words shorter than this are ignored; a 2-letter word inside a password
#: says nothing about it and would reject a large share of all passwords.
MIN_CONTEXT_WORD_LENGTH = 4

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, frozenset[str]]] = {}


def normalize_for_comparison(value: str) -> str:
    """NFKC then casefold: the form every blocklist / context comparison uses."""
    return unicodedata.normalize("NFKC", value).casefold()


def _read_entries(path: Path) -> frozenset[str]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        return frozenset(normalize_for_comparison(line.strip()) for line in handle if line.strip())


def load_blocklist(configured_path: str = "") -> frozenset[str]:
    """Return the normalised blocklist, cached per (path, mtime).

    An unreadable operator-supplied file falls back to the bundled list with an ERROR
    log rather than disabling the check: a typo in a path must not silently remove a
    control the operator turned on.
    """
    candidates: list[Path] = []
    if configured_path.strip():
        candidates.append(Path(configured_path.strip()))
    candidates.append(BUNDLED_BLOCKLIST_PATH)

    for index, path in enumerate(candidates):
        try:
            mtime = path.stat().st_mtime
            key = str(path)
            with _cache_lock:
                hit = _cache.get(key)
                if hit and hit[0] == mtime:
                    return hit[1]
            entries = _read_entries(path)
            with _cache_lock:
                _cache[key] = (mtime, entries)
            return entries
        except OSError:
            if index == 0 and len(candidates) > 1:
                logger.error(
                    "PASSWORD_BLOCKLIST_PATH %r is not readable; falling back to the "
                    "bundled common-password list",
                    str(path),
                )
                continue
            logger.exception("Bundled password blocklist %s is not readable", path)
            return frozenset()
    return frozenset()


def is_blocklisted(password: str, configured_path: str = "") -> bool:
    """True when the normalised password is an entry of the offline blocklist."""
    return normalize_for_comparison(password) in load_blocklist(configured_path)


def find_context_word(password: str, words: list[str]) -> str | None:
    """Return the first context word contained in the password, or None.

    *words* are product / account specific terms (app name, username, email local
    part, operator-configured organisation names). Case-insensitive, NFKC-normalised.
    """
    haystack = normalize_for_comparison(password)
    for word in words:
        needle = normalize_for_comparison(word.strip())
        if len(needle) >= MIN_CONTEXT_WORD_LENGTH and needle in haystack:
            return word
    return None


def pwned_count(password: str, api_url: str, timeout_seconds: float) -> int | None:
    """Occurrences of *password* in the breach corpus via k-anonymity, or None.

    ``None`` means "could not tell" (fail-open); the caller must not treat it as clean
    evidence. ``0`` means the service answered and the password was not listed.
    """
    # SHA-1 is the wire format of the range API, not a security use of the hash.
    digest = (
        hashlib.sha1(  # noqa: S324  # nosec B324
            unicodedata.normalize("NFKC", password).encode("utf-8"), usedforsecurity=False
        )
        .hexdigest()
        .upper()
    )
    prefix, suffix = digest[:5], digest[5:]
    try:
        response = httpx.get(
            f"{api_url.rstrip('/')}/{prefix}",
            headers={"Add-Padding": "true", "User-Agent": "OpenTranscribe-password-check"},
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        if response.status_code != 200:
            logger.warning(
                "Breached-password range lookup returned HTTP %s; failing open "
                "(password accepted without the online check)",
                response.status_code,
            )
            return None
        for line in response.text.splitlines():
            candidate, _, count = line.strip().partition(":")
            if candidate.upper() == suffix:
                try:
                    return int(count.strip() or "1")
                except ValueError:
                    return 1
        return 0
    except httpx.HTTPError as exc:
        logger.warning(
            "Breached-password range lookup failed (%s: %s); failing open "
            "(password accepted without the online check)",
            type(exc).__name__,
            exc,
        )
        return None
