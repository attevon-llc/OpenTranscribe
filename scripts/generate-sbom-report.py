#!/usr/bin/env python3
"""Generate a commercial-license SBOM report for backend, frontend, and base OS images.

Reads pre-generated tool output (trivy image license scans, npm license-checker JSON)
and classifies every package against a permissive-only commercial license policy.

Inputs (paths are fixed, produced by the accompanying shell steps in
security-reports/license-compliance/README.md):
    /tmp/trivy-backend.json           trivy image --scanners license (backend prod image)
    /tmp/trivy-frontend.json          trivy image --scanners license (frontend prod image)
    /tmp/license-checker-frontend-prod.json   npm license-checker --json (prod deps only)
    /tmp/license-checker-frontend-all.json    npm license-checker --json (prod+dev deps)

Outputs, under security-reports/license-compliance/:
    backend-python-licenses.csv/.md
    backend-os-packages.csv/.md
    frontend-npm-licenses.csv/.md
    frontend-os-packages.csv/.md
    SBOM-SUMMARY.md   combined attorney-facing summary with flagged items up top
"""

import csv
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / 'docs' / 'legal' / 'sbom'

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

# Packages that are pip-scan noise, not real shipped dependencies (test
# fixtures bundled inside another package's own test suite, or the project's
# own root package appearing in npm/pip tool output).
IGNORE_PACKAGES = {'my-test-package', 'opentranscribe-frontend', 'opentranscribe-backend'}

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
    # "OR" expressions are satisfiable by the best available option;
    # "AND" expressions require every listed license to be acceptable.
    or_parts = re.split(r'\bOR\b|\|', text, flags=re.IGNORECASE)
    if len(or_parts) > 1:
        results = [classify_license_string(p) for p in or_parts]
        cats = [r[0] for r in results]
        if 'allow' in cats:
            return 'allow', "satisfied by an OR'd permissive option: " + text
        if 'concern' in cats:
            return 'concern', 'OR expression, best option is weak-copyleft: ' + text
        return 'blocker', 'no acceptable option in OR expression: ' + text

    and_parts = re.split(r'\bAND\b', text, flags=re.IGNORECASE)
    if len(and_parts) > 1:
        results = [classify_license_string(p) for p in and_parts]
        severity = ['allow', 'concern', 'proprietary', 'unknown', 'blocker']
        worst = max(results, key=lambda r: severity.index(r[0]))
        return worst[0], 'AND expression, worst component governs: ' + text

    tok = normalize_token(text)
    tok = re.sub(r'\s+', '-', tok)
    if tok in ALLOW:
        return 'allow', 'permissive'
    if tok in CONCERN:
        return 'concern', 'weak copyleft'
    if tok in BLOCKER_EXACT:
        return 'blocker', 'strong copyleft / source-available / no-license'
    # loose contains-checks for common variants trivy/license-checker emit
    if any(tok.startswith(p) for p in ('gpl-', 'agpl-')):
        return 'blocker', 'GPL-family'
    if tok.startswith('lgpl-'):
        return 'concern', 'LGPL-family'
    if tok.startswith('mpl-'):
        return 'concern', 'MPL-family'
    if tok in ALLOW:
        return 'allow', 'permissive'
    return 'unknown', f'unrecognized license identifier: {text}'


def write_csv(rows: list[dict], path: Path, fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_md_table(rows: list[dict], path: Path, fields: list[str], title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'# {title}', '']
    lines.append('| ' + ' | '.join(fields) + ' |')
    lines.append('|' + '|'.join(['---'] * len(fields)) + '|')
    for r in rows:
        lines.append(
            '| ' + ' | '.join(str(r.get(f, '')).replace('|', '\\|') for f in fields) + ' |'
        )
    path.write_text('\n'.join(lines) + '\n')


# --- Backend: Python packages (from trivy image scan of the shipped image) --
def load_backend_python() -> list[dict]:
    data = json.loads(Path('/tmp/trivy-backend.json').read_text())
    rows_by_pkg: dict[str, set[str]] = {}
    for r in data.get('Results', []):
        if r.get('Class') == 'license' and r.get('Target') == 'Python':
            for lic in r.get('Licenses', []):
                name = lic.get('PkgName', '')
                rows_by_pkg.setdefault(name, set()).add(lic.get('Name', ''))

    # Cross-reference authors/homepage from local pip-licenses run where available.
    authors: dict[str, dict] = {}
    pl_path = Path('/tmp/pip-licenses-all.json')
    if pl_path.exists():
        for entry in json.loads(pl_path.read_text()):
            authors[entry['Name'].lower()] = entry

    out = []
    for pkg, licenses in sorted(rows_by_pkg.items()):
        if pkg.lower() in IGNORE_PACKAGES:
            continue
        lic_str = ' OR '.join(sorted(licenses)) if len(licenses) > 1 else next(iter(licenses), '')
        cat, reason = classify_license_string(lic_str)
        meta = authors.get(pkg.lower(), {})
        out.append(
            {
                'Package': pkg,
                'Author': meta.get('Author', ''),
                'License': lic_str or 'UNKNOWN',
                'Status': CATEGORY_LABEL[cat],
                'Reason': reason,
                'URL': meta.get('URL', ''),
            }
        )
    return out


# --- OS packages (Debian for backend image, Alpine for frontend image) ------
def load_os_packages(trivy_json_path: str) -> list[dict]:
    data = json.loads(Path(trivy_json_path).read_text())
    rows_by_pkg: dict[str, set[str]] = {}
    for r in data.get('Results', []):
        if r.get('Class') == 'license' and r.get('Target') == 'OS Packages':
            for lic in r.get('Licenses', []):
                name = lic.get('PkgName', '')
                rows_by_pkg.setdefault(name, set()).add(lic.get('Name', ''))

    out = []
    for pkg, licenses in sorted(rows_by_pkg.items()):
        if pkg.lower() in IGNORE_PACKAGES:
            continue
        lic_str = ' AND '.join(sorted(licenses)) if len(licenses) > 1 else next(iter(licenses), '')
        cat, reason = classify_license_string(lic_str)
        out.append(
            {
                'Package': pkg,
                'License': lic_str or 'UNKNOWN',
                'Status': CATEGORY_LABEL[cat],
                'Reason': reason,
            }
        )
    return out


# --- Frontend: npm packages (from license-checker) ---------------------------
def load_npm(license_checker_json_path: str) -> list[dict]:
    data = json.loads(Path(license_checker_json_path).read_text())
    out = []
    for key, meta in sorted(data.items()):
        # key looks like "package-name@1.2.3"
        name, _, version = key.rpartition('@')
        if not name:
            name, version = key, ''
        if name.lower() in IGNORE_PACKAGES:
            continue
        lic_field = meta.get('licenses', '')
        lic_str = ' AND '.join(lic_field) if isinstance(lic_field, list) else str(lic_field)
        cat, reason = classify_license_string(lic_str)
        out.append(
            {
                'Package': name,
                'Version': version,
                'License': lic_str or 'UNKNOWN',
                'Status': CATEGORY_LABEL[cat],
                'Reason': reason,
                'Repository': meta.get('repository', ''),
                'Publisher': meta.get('publisher', ''),
            }
        )
    return out


STATUS_COLUMNS = ['OK', 'REVIEW', 'PROPRIETARY-REVIEW', 'BLOCKER', 'UNKNOWN']


def summarize(rows: list[dict]) -> dict:
    counts = dict.fromkeys(STATUS_COLUMNS, 0)
    for r in rows:
        counts[r['Status']] = counts.get(r['Status'], 0) + 1
    return counts


def main() -> None:
    backend_py = load_backend_python()
    backend_os = load_os_packages('/tmp/trivy-backend.json')
    frontend_os = load_os_packages('/tmp/trivy-frontend.json')
    frontend_npm_prod = load_npm('/tmp/license-checker-frontend-prod.json')
    frontend_npm_all = load_npm('/tmp/license-checker-frontend-all.json')

    write_csv(
        backend_py,
        OUT_DIR / 'backend-python-licenses.csv',
        ['Package', 'Author', 'License', 'Status', 'Reason', 'URL'],
    )
    write_md_table(
        backend_py,
        OUT_DIR / 'backend-python-licenses.md',
        ['Package', 'Author', 'License', 'Status'],
        'Backend — Python package licenses (production image, davidamacey/opentranscribe-backend)',
    )

    write_csv(
        backend_os, OUT_DIR / 'backend-os-packages.csv', ['Package', 'License', 'Status', 'Reason']
    )
    write_md_table(
        backend_os,
        OUT_DIR / 'backend-os-packages.md',
        ['Package', 'License', 'Status'],
        'Backend — Debian 13 (trixie) OS package licenses (base image layer)',
    )

    write_csv(
        frontend_npm_prod,
        OUT_DIR / 'frontend-npm-licenses.csv',
        ['Package', 'Version', 'License', 'Status', 'Reason', 'Repository', 'Publisher'],
    )
    write_md_table(
        frontend_npm_prod,
        OUT_DIR / 'frontend-npm-licenses.md',
        ['Package', 'Version', 'License', 'Status'],
        'Frontend — npm production dependency licenses (bundled into the shipped SPA)',
    )

    write_csv(
        frontend_npm_all,
        OUT_DIR / 'frontend-npm-licenses-including-devtools.csv',
        ['Package', 'Version', 'License', 'Status', 'Reason', 'Repository', 'Publisher'],
    )

    write_csv(
        frontend_os,
        OUT_DIR / 'frontend-os-packages.csv',
        ['Package', 'License', 'Status', 'Reason'],
    )
    write_md_table(
        frontend_os,
        OUT_DIR / 'frontend-os-packages.md',
        ['Package', 'License', 'Status'],
        'Frontend — Alpine OS package licenses (nginx:1.31.2-alpine3.23 base image)',
    )

    datasets = {
        'Backend Python packages (production image)': backend_py,
        'Backend OS packages (Debian, base image)': backend_os,
        'Frontend npm packages (production, bundled into SPA)': frontend_npm_prod,
        'Frontend OS packages (Alpine, base image)': frontend_os,
    }

    flagged = []
    for label, rows in datasets.items():
        for r in rows:
            if r['Status'] in ('BLOCKER', 'UNKNOWN'):
                flagged.append({'Area': label, **r})

    proprietary = []
    for label, rows in datasets.items():
        for r in rows:
            if r['Status'] == 'PROPRIETARY-REVIEW':
                proprietary.append({'Area': label, **r})

    summary_lines = [
        '# Software Bill of Materials — Commercial License Review',
        '',
        f'Generated: {__import__("datetime").date.today().isoformat()}',
        'Scope: OpenTranscribe backend (Python), frontend (npm/SvelteKit), and the Debian/Alpine',
        'base OS packages baked into the production Docker images actually published to Docker Hub',
        '(`davidamacey/opentranscribe-backend:v0.5.1`, `davidamacey/opentranscribe-frontend:v0.5.1`).',
        '',
        '**Policy applied** (standard permissive-only, confirmed 2026-09-24):',
        '- **OK** — permissive license (MIT, Apache-2.0, BSD, ISC, etc.), no restriction on closed-source commercial use.',
        '- **REVIEW** — weak copyleft (LGPL, MPL, CDDL, EPL) — generally fine when used as an unmodified,',
        '  dynamically-linked/external dependency, but has distribution obligations attorneys should confirm',
        '  for the specific way each package is used (static linking, source modification, bundling).',
        '- **PROPRIETARY-REVIEW** — a vendor EULA, not open source (e.g. NVIDIA CUDA runtime libraries,',
        '  Microsoft Azure Speech SDK). These are routinely redistributed inside commercial products under',
        "  the vendor's own redistribution terms — NVIDIA's CUDA EULA, for example, explicitly permits",
        "  redistributing the listed runtime libraries with an application — but each vendor's terms need a",
        "  one-time confirmation, and the redistribution terms are the vendor's EULA, not an OSI license.",
        '- **BLOCKER** — strong copyleft (GPL/AGPL) or a source-available/no-commercial license (SSPL,',
        '  Business Source License, Commons Clause) — not viable for closed-source commercial distribution',
        '  without a separate license from the author, or requires open-sourcing derivative work.',
        '- **UNKNOWN** — no machine-readable license metadata found; needs manual lookup before ship.',
        '',
        '⚠️ **Important distinction for the OS-package tables below**: the large BLOCKER counts there are',
        'GPL-licensed Linux system utilities (bash, coreutils, dpkg, tar, gzip, etc.) that come with every',
        'Debian/Alpine base image and are invoked as separate OS-level programs — they are not linked into,',
        'compiled with, or modified as part of the application source. This is the same base every major',
        'commercial SaaS product running in a Linux container relies on. It is legally distinct from a GPL',
        "**library directly imported into the application's own source code**, which is the higher-risk",
        'pattern and is called out separately per language ecosystem below (see `mutagen` in the Python table).',
        'Attorneys should confirm this distinction applies as expected, not treat every GPL row as blocking.',
        '',
        '**One named exception worth flagging specifically**: `ffmpeg` (GPL, in the OS-package table below) is',
        'not incidental base-image cruft — the backend deliberately invokes it for audio/video processing.',
        'Confirmed via `backend/app/tasks/transcription/waveform_generator.py` and the `ffmpeg-python` wrapper',
        '(`backend/app/utils/thumbnail.py`, `backend/app/tasks/transcription/audio_processor.py`): the app',
        'always calls the `ffmpeg` binary as a separate subprocess (fork/exec), never links `libavcodec`/`libavformat`',
        'into the Python process. Same "separate program invoked externally" category as the other OS utilities,',
        'but attorneys should confirm this specific one since it is functionally load-bearing, not incidental.',
        '',
        '## Summary by area',
        '',
        '| Area | OK | REVIEW | PROPRIETARY-REVIEW | BLOCKER | UNKNOWN | Total |',
        '|---|---|---|---|---|---|---|',
    ]
    for label, rows in datasets.items():
        c = summarize(rows)
        total = len(rows)
        summary_lines.append(
            f'| {label} | {c["OK"]} | {c["REVIEW"]} | {c["PROPRIETARY-REVIEW"]} | {c["BLOCKER"]} | {c["UNKNOWN"]} | {total} |'
        )

    summary_lines += [
        '',
        '## Application-level dependencies flagged BLOCKER (GPL/AGPL library imported directly into app code)',
        '',
        'These are the highest-priority items — unlike the OS-package GPL utilities above, these are',
        'libraries the application code directly imports/links against.',
        '',
    ]
    app_blockers = [
        {'Area': label, **r}
        for label, rows in datasets.items()
        for r in rows
        if r['Status'] == 'BLOCKER' and 'OS package' not in label
    ]
    if not app_blockers:
        summary_lines.append('None found.')
    else:
        summary_lines.append('| Area | Package | License | Reason |')
        summary_lines.append('|---|---|---|---|')
        for r in app_blockers:
            summary_lines.append(
                f'| {r["Area"]} | {r["Package"]} | {r["License"]} | {r.get("Reason", "")} |'
            )
        summary_lines.append('')
        summary_lines.append(
            '`mutagen` provenance: not a direct dependency — pulled in transitively as the '
            '`mutagen (metadata)` extra of `yt-dlp[default]` (`backend/requirements.txt:238`), used '
            'for reading audio tag metadata during video/audio ingestion. Confirm whether `yt-dlp` '
            'can be installed with that extra disabled, or whether GPL-2.0-or-later is acceptable '
            'for this specific usage (metadata *reading*, not modification/redistribution of `mutagen` itself).'
        )

    summary_lines += ['', '## Vendor EULA dependencies (PROPRIETARY-REVIEW)', '']
    if not proprietary:
        summary_lines.append('None found.')
    else:
        summary_lines.append('| Area | Package | License |')
        summary_lines.append('|---|---|---|')
        for r in proprietary:
            summary_lines.append(f'| {r["Area"]} | {r["Package"]} | {r["License"]} |')

    summary_lines += [
        '',
        '## Full flagged list, including OS-layer GPL utilities (BLOCKER + UNKNOWN)',
        '',
    ]
    if not flagged:
        summary_lines.append('None found.')
    else:
        summary_lines.append('| Area | Package | License | Status | Reason |')
        summary_lines.append('|---|---|---|---|---|')
        for r in flagged:
            summary_lines.append(
                f'| {r["Area"]} | {r["Package"]} | {r["License"]} | {r["Status"]} | {r.get("Reason", "")} |'
            )

    summary_lines += [
        '',
        '## REVIEW items (weak copyleft — confirm usage pattern)',
        '',
        '| Area | Package | License |',
        '|---|---|---|',
    ]
    for label, rows in datasets.items():
        for r in rows:
            if r['Status'] == 'REVIEW':
                summary_lines.append(f'| {label} | {r["Package"]} | {r["License"]} |')

    summary_lines += [
        '',
        '## Full detail',
        '',
        'Per-package CSV/Markdown files in this directory:',
        '- `backend-python-licenses.csv` / `.md`',
        '- `backend-os-packages.csv` / `.md`',
        '- `frontend-npm-licenses.csv` / `.md` (production dependencies — what actually ships in the built SPA)',
        '- `frontend-npm-licenses-including-devtools.csv` (build-time tooling only, not shipped; included for completeness)',
        '- `frontend-os-packages.csv` / `.md`',
        '',
        '## Regenerating this report',
        '',
        'See `security-reports/license-compliance/README.md` for the exact commands. This report reflects a point-in-time',
        'scan of the images tagged above — re-run it whenever dependencies change or before each release.',
    ]

    (OUT_DIR / 'SBOM-SUMMARY.md').write_text('\n'.join(summary_lines) + '\n')

    print(f'Wrote report to {OUT_DIR}')
    for label, rows in datasets.items():
        c = summarize(rows)
        print(
            f'  {label}: OK={c["OK"]} REVIEW={c["REVIEW"]} BLOCKER={c["BLOCKER"]} UNKNOWN={c["UNKNOWN"]}'
        )
    print(f'  Flagged for attorney review: {len(flagged)}')


if __name__ == '__main__':
    main()
