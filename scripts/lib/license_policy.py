"""Commercial-use licence classification policy (permissive-only), stdlib only.

Single home of the ALLOW / CONCERN / BLOCKER policy. ``classify_license_string`` returns
``(category, reason)`` with category one of ``allow``, ``concern``, ``proprietary``,
``unknown``, ``blocker``; ``CATEGORY_LABEL`` maps it to the report status column. Anything
unrecognised or empty is never ``allow``.
"""

import re

# --- Commercial-use policy (permissive-only) -------------------------------
# Confirmed with the user 2026-09-24: standard permissive-only policy.
#   ALLOW    - permissive, no restrictions on closed-source commercial use
#   CONCERN  - weak copyleft; usable but has linking/distribution obligations
#              attorneys should confirm (esp. for statically-linked/embedded use)
#   BLOCKER  - strong copyleft, source-available-with-restrictions, or "no
#              license granted" - not viable for closed-source commercial use
#              without a separate commercial license from the author
ALLOW = {
    'mit',
    'mit-0',
    'apache-2.0',
    'apache2.0',
    'apache 2.0',
    'apache-1.1',
    'bsd',
    'bsd-2-clause',
    'bsd-3-clause',
    'bsd-3-clause-clear',
    'bsd-4-clause',
    'isc',
    '0bsd',
    'unlicense',
    'cc0-1.0',
    'cc0',
    'python-2.0',
    'psf-2.0',
    'psf',
    'zlib',
    'zlib/libpng',
    'libpng',
    'x11',
    'wtfpl',
    'bsl-1.0',
    'boost-1.0',
    'blessing',
    'ofl-1.1',
    'mit-cmu',
    'ncsa',
    'vovida',
    'postgresql',
    'ruby',
    'artistic-2.0',
    'cnri-python',
    'python-software-foundation-license',
    'python software foundation license',
}

# Vendor EULA / "Other/Proprietary License" classifiers (NVIDIA CUDA runtime
# libraries, Microsoft Azure SDKs, etc). These are NOT copyleft and are
# routinely redistributed in commercial products under the vendor's own
# redistribution terms (e.g. the NVIDIA CUDA EULA explicitly permits
# redistributing the runtime libraries with an application) - but they are
# also not open source, so they get their own category rather than being
# lumped in with GPL/AGPL "BLOCKER" or plain "UNKNOWN".
PROPRIETARY_MARKERS = (
    'other/proprietary license',
    'proprietary software',
    'licenseref-nvidia',
    'nvidia proprietary',
    'licenseref-',
    'commercial',
)

CONCERN = {
    'lgpl-2.0',
    'lgpl-2.0-only',
    'lgpl-2.0-or-later',
    'lgpl-2.1',
    'lgpl-2.1-only',
    'lgpl-2.1-or-later',
    'lgpl-3.0',
    'lgpl-3.0-only',
    'lgpl-3.0-or-later',
    'mpl-1.1',
    'mpl-2.0',
    'epl-1.0',
    'epl-2.0',
    'cddl-1.0',
    'cddl-1.1',
    'cc-by-sa-4.0',
    'cc-by-sa-3.0',
    'cc-by-4.0',
    'cc-by-3.0',
    'openssl',
    'apsl-2.0',
}

BLOCKER_EXACT = {
    'gpl-1.0',
    'gpl-2.0',
    'gpl-2.0-only',
    'gpl-2.0-or-later',
    'gpl-3.0',
    'gpl-3.0-only',
    'gpl-3.0-or-later',
    'agpl-1.0',
    'agpl-3.0',
    'agpl-3.0-only',
    'agpl-3.0-or-later',
    'sspl-1.0',
    'bsl-1.1',  # NOTE: BSL-1.1 (Business Source License) != BSL-1.0 (Boost)
    'commons-clause',
    'unlicensed',
    'proprietary',
    'unknown',
    'noassertion',
    '',
}
# Substrings that mean "blocker" wherever they appear in a license string,
# so we still catch e.g. "GPL-2.0-or-later WITH Bison-exception" style riders.
BLOCKER_SUBSTRINGS = ('sspl', 'commons-clause', 'business source', 'busl')

CATEGORY_LABEL = {
    'allow': 'OK',
    'concern': 'REVIEW',
    'blocker': 'BLOCKER',
    'unknown': 'UNKNOWN',
    'proprietary': 'PROPRIETARY-REVIEW',
}


def normalize_token(tok: str) -> str:
    return tok.strip().strip('()').lower()


def _classify_or(text: str, parts: list[str]) -> tuple[str, str]:
    """An OR expression is satisfied by its best option."""
    cats = [classify_license_string(p)[0] for p in parts]
    if 'allow' in cats:
        return 'allow', "satisfied by an OR'd permissive option: " + text
    if 'concern' in cats:
        return 'concern', 'OR expression, best option is weak-copyleft: ' + text
    return 'blocker', 'no acceptable option in OR expression: ' + text


def _classify_and(text: str, parts: list[str]) -> tuple[str, str]:
    """An AND expression needs every component acceptable, so the worst one governs."""
    severity = ['allow', 'concern', 'proprietary', 'unknown', 'blocker']
    worst = max((classify_license_string(p) for p in parts), key=lambda r: severity.index(r[0]))
    return worst[0], 'AND expression, worst component governs: ' + text


def _classify_token(text: str) -> tuple[str, str]:
    tok = re.sub(r'\s+', '-', normalize_token(text))
    if tok in ALLOW:
        return 'allow', 'permissive'
    if tok in CONCERN:
        return 'concern', 'weak copyleft'
    if tok in BLOCKER_EXACT:
        return 'blocker', 'strong copyleft / source-available / no-license'
    # loose prefix checks for common variants trivy/license-checker emit
    if tok.startswith(('gpl-', 'agpl-')):
        return 'blocker', 'GPL-family'
    if tok.startswith('lgpl-'):
        return 'concern', 'LGPL-family'
    if tok.startswith('mpl-'):
        return 'concern', 'MPL-family'
    return 'unknown', f'unrecognized license identifier: {text}'


def classify_license_string(raw: str | None) -> tuple[str, str]:
    """Return (category, reason) for a raw license expression string."""
    if not raw or not raw.strip():
        return 'unknown', 'no license metadata found'

    text = raw.strip()
    low = text.lower()
    for sub in BLOCKER_SUBSTRINGS:
        if sub in low:
            return 'blocker', f"matches restricted term '{sub}'"
    for marker in PROPRIETARY_MARKERS:
        if marker in low:
            return 'proprietary', f"vendor EULA / proprietary license marker '{marker}'"

    # SPDX-ish expressions: split on OR / AND, tolerate commas and parens.
    or_parts = re.split(r'\bOR\b|\|', text, flags=re.IGNORECASE)
    if len(or_parts) > 1:
        return _classify_or(text, or_parts)
    and_parts = re.split(r'\bAND\b', text, flags=re.IGNORECASE)
    if len(and_parts) > 1:
        return _classify_and(text, and_parts)
    return _classify_token(text)
