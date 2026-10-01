"""Common / breached-password screening (NIST SP 800-63B-4 section 3.1.1.2).

Two independent checks, both fed the *NFKC-normalised* password so a full-width or
ligature spelling of a listed password does not slip past:

* an **offline** blocklist - a file of SHA-1 hashes that the operator installs with
  ``./opentranscribe.sh download-models password-blocklist`` (Have I Been Pwned
  "Pwned Passwords", top 100,000 by breach count; see ``build_password_blocklist.py`` and
  the password-policy docs). It is read from ``<models dir>/password-blocklist/`` or from
  ``PASSWORD_BLOCKLIST_PATH`` (a hash file or a legacy plaintext file). It works on
  air-gapped hosts and never leaves the host. **No list is shipped in the repository**: when
  the file is absent the check is skipped, with one WARNING, and the context-word check
  still applies.
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
import json
import logging
import re
import threading
import unicodedata
from pathlib import Path

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_BLOCKLIST_FILENAME = "pwned-passwords-top100k-sha1.txt"
INSTALL_COMMAND = "./opentranscribe.sh download-models password-blocklist"


def default_blocklist_path() -> Path:
    """Where ``download-models password-blocklist`` writes the list inside the container."""
    return Path(settings.MODEL_BASE_DIR) / "password-blocklist" / DEFAULT_BLOCKLIST_FILENAME


_SHA1_LINE = re.compile(r"^[0-9A-Fa-f]{40}$")

#: Context words shorter than this are ignored; a 2-letter word inside a password
#: says nothing about it and would reject a large share of all passwords.
MIN_CONTEXT_WORD_LENGTH = 4

_missing_warned = False
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, frozenset[str]]] = {}


def normalize_for_comparison(value: str) -> str:
    """NFKC then casefold: the form every blocklist / context comparison uses."""
    return unicodedata.normalize("NFKC", value).casefold()


def _sha1_hex(value: str) -> str:
    # SHA-1 is the Pwned Passwords identifier format, not a security use of the hash.
    return (
        hashlib.sha1(value.encode("utf-8"), usedforsecurity=False)  # noqa: S324  # nosec B324
        .hexdigest()
        .upper()
    )


def _candidate_forms(password: str) -> set[str]:
    """Forms of the password that are looked up: NFKC as typed, lowercased, casefolded.

    Pwned Passwords hashes are case-exact, so a list entry for ``password`` would not
    match ``PASSWORD`` by itself; the lowercase forms close that gap.
    """
    nfkc = unicodedata.normalize("NFKC", password)
    return {nfkc, nfkc.lower(), nfkc.casefold()}


def _read_entries(path: Path) -> frozenset[str]:
    """Read a blocklist file into a set of uppercase SHA-1 hex digests.

    Each line is auto-detected: 40 hex characters is taken as a SHA-1 digest (optionally
    followed by ``:count``, the Pwned Passwords format); anything else is a legacy
    plaintext entry, which is casefolded after NFKC and hashed here.
    """
    entries: set[str] = set()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            head = line.partition(":")[0]
            if _SHA1_LINE.match(head):
                entries.add(head.upper())
            else:
                entries.add(_sha1_hex(normalize_for_comparison(line)))
    return frozenset(entries)


def _warn_missing_once(path: Path) -> None:
    global _missing_warned
    with _cache_lock:
        if _missing_warned:
            return
        _missing_warned = True
    logger.warning(
        "Breached-password list not installed (%s); the common-password check is skipped. "
        "Install it with: %s",
        path,
        INSTALL_COMMAND,
    )


def _read_cached(path: Path) -> frozenset[str]:
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


def load_blocklist(configured_path: str = "") -> frozenset[str]:
    """Return the set of blocklisted SHA-1 digests, cached per (path, mtime).

    An unreadable operator-supplied file falls back to the default location with an
    ERROR log: a typo in a path must not silently remove a control the operator turned
    on. When no list is installed at all the result is empty (check skipped, one
    WARNING) - the app never fails to start or to register a user over it.
    """
    configured = configured_path.strip()
    if configured:
        try:
            return _read_cached(Path(configured))
        except OSError:
            logger.error(
                "PASSWORD_BLOCKLIST_PATH %r is not readable; falling back to the default list",
                configured,
            )
    path = default_blocklist_path()
    try:
        return _read_cached(path)
    except FileNotFoundError:
        _warn_missing_once(path)
    except OSError:
        logger.exception("Password blocklist %s is not readable", path)
    return frozenset()


def blocklist_status(configured_path: str = "") -> dict[str, object]:
    """Whether a list is installed, how large it is and where it came from.

    ``retrieved`` comes from the ``<file>.meta.json`` sidecar the builder writes, when present.
    """
    configured = configured_path.strip()
    path = Path(configured) if configured else default_blocklist_path()
    if configured and not path.is_file():
        path = default_blocklist_path()
        configured = ""
    entries = load_blocklist(configured)
    status: dict[str, object] = {
        "installed": bool(entries),
        "entries": len(entries),
        "source": "custom" if configured else "default",
        "retrieved": None,
    }
    try:
        meta = json.loads(path.with_name(path.name + ".meta.json").read_text(encoding="utf-8"))
        status["retrieved"] = str(meta.get("retrieved") or "") or None
    except (OSError, ValueError, AttributeError):
        pass
    return status


def log_startup_status(enabled: bool, configured_path: str = "") -> None:
    """One startup line: WARNING when the check is on but no list is installed."""
    if not enabled:
        return
    status = blocklist_status(configured_path)
    if status["installed"]:
        logger.info("Password blocklist loaded: %s hashes", status["entries"])
    # load_blocklist() has already emitted the single "not installed" WARNING.


def is_blocklisted(password: str, configured_path: str = "") -> bool:
    """True when the SHA-1 of the NFKC password, or of its lowercased form, is listed."""
    entries = load_blocklist(configured_path)
    return any(_sha1_hex(form) in entries for form in _candidate_forms(password))


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
    digest = _sha1_hex(unicodedata.normalize("NFKC", password))
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
