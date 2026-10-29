#!/usr/bin/env python3
"""Check the security headers, cookies and TLS posture of a deployed OpenTranscribe URL.

Run it by hand after each public deploy, against a ``--fresh`` stack, or from
``scripts/public-smoke.sh``. Stdlib only. It enforces what ``nginx/site.conf.template``
sets, so a regression there (or a ``location`` block whose own ``add_header`` silently
drops the inherited ones) becomes a finding instead of a quiet gap.

Requests (never followed through redirects, never authenticated, body capped at 64 KB):
    GET <base>/                    expected status 200
    GET <base>/api/auth/session    expected status 200 (anonymous probe)
    GET <base>/api/auth/me         expected status 401 (protected, no cookies)
    GET <http-url>/                https bases only: a 301/302/303/307/308 to https on the
                                   same host
Any other status is a finding, so an outage page with good headers never prints OK.

Enforced on every response above:
    Strict-Transport-Security   https bases only. The first header must carry exactly one
                                ``max-age=<digits>`` directive >= 15552000 (no ``x-max-age``,
                                no trailing garbage, a repeated max-age is a finding)
    Content-Security-Policy     present. Per RFC the first occurrence of a directive wins.
                                With several CSP headers the browser enforces all of them, so
                                a weakness counts only if EVERY policy has it. Findings: no
                                default-src/script-src; ``*`` or ``data:`` in the effective
                                script sources; ``'unsafe-eval'``; bare ws:/wss:/http:/https:
                                source (same rule as frontend/scripts/check-built-csp.mjs);
                                object-src not 'none'; ``frame-ancestors *``
    X-Content-Type-Options      nosniff
    Frame protection            a CSP frame-ancestors without ``*`` (any policy), or
                                X-Frame-Options DENY/SAMEORIGIN
    Referrer-Policy             present, no ``unsafe-url`` / ``no-referrer-when-downgrade``
                                anywhere in the list, at least one valid token
    Permissions-Policy          present and non-empty
    Server / X-Powered-By       Server must not disclose ``name/major.minor`` software
                                versions (``awselb/2.0`` is allowed); no X-Powered-By
    Set-Cookie (any observed)   Secure (https bases), HttpOnly (except csrf_token, which the
                                SPA reads), SameSite=Lax|Strict
TLS (https bases): negotiated protocol >= TLS 1.2; a second handshake capped at TLS 1.1 must
be REJECTED; the certificate verifies and has more than 14 days left.

Limits, reported as NOTE lines and never as a pass:
    * The anonymous requests set no cookie, so cookie flags are only verified when a
      response happens to carry one. ``cookies: none observed (UNVERIFIED)`` means the
      Secure/HttpOnly/SameSite rules were NOT exercised; verify them from a logged-in
      browser session (devtools, Application > Cookies).
    * A deployment that ships its CSP only as a ``<meta>`` tag (no nginx in front) reports
      the header as missing.
    * ``TLS 1.0/1.1 ... UNVERIFIED`` means the local OpenSSL cannot offer those versions.

Exit codes: 0 all checks passed, 1 findings, 2 target unreachable, hung past ``--deadline``
or given an unusable URL. An error is never a pass.

Examples:
    scripts/check-public-headers.py https://opentranscribe.example.com
    scripts/check-public-headers.py http://127.0.0.1:5373 --allow-http   # fresh stack
"""

from __future__ import annotations

import argparse
import http.client
import re
import signal
import socket
import ssl
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import FrameType
from urllib.parse import urlsplit

MIN_HSTS_MAX_AGE = 15552000
MIN_CERT_DAYS = 14
BARE_NETWORK_SCHEMES = ('ws:', 'wss:', 'http:', 'https:')
CSRF_COOKIE = 'csrf_token'
EXPECTED_STATUS = {'/': 200, '/api/auth/session': 200, '/api/auth/me': 401}
TIMEOUT_SECONDS = 15.0
DEFAULT_DEADLINE_SECONDS = 60
MAX_BODY_BYTES = 64 * 1024
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
VALID_REFERRER_TOKENS = frozenset(
    {
        '',
        'no-referrer',
        'no-referrer-when-downgrade',
        'same-origin',
        'origin',
        'strict-origin',
        'origin-when-cross-origin',
        'strict-origin-when-cross-origin',
        'unsafe-url',
    }
)
UNSAFE_REFERRER_TOKENS = frozenset({'unsafe-url', 'no-referrer-when-downgrade'})
SERVER_VERSION = re.compile(r'[A-Za-z][\w.-]*/\d+\.\d+')
BENIGN_SERVER_VALUES = frozenset({'awselb/2.0'})

EXIT_PASS = 0
EXIT_FINDINGS = 1
EXIT_UNUSABLE = 2


class TargetUnusable(Exception):
    """The target could not be reached or answered something unusable."""


@dataclass
class Response:
    """One HTTP response: status plus every header as an ordered (name, value) pair."""

    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)

    def get(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    def get_all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]


@dataclass
class TlsInfo:
    """What the TLS handshakes revealed.

    ``legacy_tls`` is True when a TLS 1.0/1.1-only client was accepted, False when it was
    rejected, None when the local OpenSSL cannot make that attempt.
    """

    version: str
    not_after: float | None  # epoch seconds
    verified: bool = True
    verify_error: str = ''
    legacy_tls: bool | None = False


Fetcher = Callable[[str], Response]
TlsProbe = Callable[[str, int], TlsInfo]


def http_fetch(url: str, insecure: bool = False) -> Response:
    """GET *url* without following redirects. Raises TargetUnusable on any transport error."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ''
        path = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
        conn: http.client.HTTPConnection
        if parts.scheme == 'https':
            ctx = ssl.create_default_context()
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(
                host, parts.port or 443, timeout=TIMEOUT_SECONDS, context=ctx
            )
        else:
            conn = http.client.HTTPConnection(host, parts.port or 80, timeout=TIMEOUT_SECONDS)
        try:
            conn.request('GET', path, headers={'User-Agent': 'check-public-headers'})
            resp = conn.getresponse()
            resp.read(MAX_BODY_BYTES)
            return Response(resp.status, list(resp.getheaders()))
        finally:
            conn.close()
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as exc:
        raise TargetUnusable(f'{url}: {type(exc).__name__}: {exc}') from exc


def _connect(host: str, port: int) -> socket.socket:
    return socket.create_connection((host, port), timeout=TIMEOUT_SECONDS)


def legacy_tls_accepted(host: str, port: int) -> bool | None:
    """Try a handshake capped at TLS 1.1. True: accepted (bad). None: cannot be tested here."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers('DEFAULT:@SECLEVEL=0')
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.maximum_version = ssl.TLSVersion.TLSv1_1
    except (ValueError, ssl.SSLError):
        return None
    try:
        with ctx.wrap_socket(_connect(host, port), server_hostname=host):
            return True
    except (OSError, ssl.SSLError):
        return False


def tls_probe(host: str, port: int) -> TlsInfo:
    """Handshake with *host*:*port*; report version, certificate expiry and legacy-TLS support."""
    legacy = legacy_tls_accepted(host, port)
    ctx = ssl.create_default_context()
    try:
        with ctx.wrap_socket(_connect(host, port), server_hostname=host) as tls:
            cert = tls.getpeercert() or {}
            return TlsInfo(tls.version() or '', _not_after(cert), True, '', legacy)
    except ssl.SSLCertVerificationError as exc:
        error = str(exc)
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise TargetUnusable(f'{host}:{port}: TLS handshake failed: {exc}') from exc
    # The chain failed. Learn the protocol version anyway; expiry is unknowable with the
    # stdlib once verification is off, so it is left unset.
    ctx2 = ssl.create_default_context()
    ctx2.check_hostname = False
    ctx2.verify_mode = ssl.CERT_NONE
    try:
        with ctx2.wrap_socket(_connect(host, port), server_hostname=host) as tls:
            return TlsInfo(tls.version() or '', None, False, error, legacy)
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise TargetUnusable(f'{host}:{port}: TLS handshake failed: {exc}') from exc


def _not_after(cert: Mapping[str, object]) -> float | None:
    raw = cert.get('notAfter')
    return float(ssl.cert_time_to_seconds(raw)) if isinstance(raw, str) else None


def parse_csp(policy: str) -> dict[str, list[str]]:
    """Directive map; a repeated directive keeps its FIRST occurrence, as browsers do."""
    directives: dict[str, list[str]] = {}
    for part in policy.split(';'):
        tokens = part.split()
        if tokens and tokens[0].lower() not in directives:
            directives[tokens[0].lower()] = tokens[1:]
    return directives


def csp_problems(policy: str) -> set[str]:
    """Weaknesses of ONE policy, as stable messages (so policies can be intersected)."""
    directives = parse_csp(policy)
    problems: set[str] = set()
    script = directives.get('script-src', directives.get('default-src'))
    if script is None:
        problems.add('CSP sets neither default-src nor script-src')
    else:
        lowered = [s.lower() for s in script]
        if '*' in lowered:
            problems.add('CSP script sources allow wildcard *')
        if 'data:' in lowered:
            problems.add('CSP script sources allow data:')
    for name, sources in directives.items():
        for source in sources:
            if source.lower() == "'unsafe-eval'":
                problems.add(f"CSP {name} allows 'unsafe-eval'")
            if source.lower() in BARE_NETWORK_SCHEMES:
                problems.add(f'CSP {name} has bare scheme source {source}')
    object_src = directives.get('object-src', directives.get('default-src', []))
    if [s.lower() for s in object_src] != ["'none'"]:
        problems.add("CSP object-src is not 'none'")
    if '*' in directives.get('frame-ancestors', []):
        problems.add('CSP frame-ancestors allows wildcard *')
    return problems


def check_csp(resp: Response, label: str) -> list[str]:
    values = resp.get_all('Content-Security-Policy')
    if not values:
        return [f'{label}: Content-Security-Policy missing']
    effective = set.intersection(*(csp_problems(v) for v in values))
    return [f'{label}: {p}' for p in sorted(effective)]


def check_hsts(resp: Response, label: str) -> list[str]:
    value = resp.get('Strict-Transport-Security')
    if value is None:
        return [f'{label}: Strict-Transport-Security missing']
    ages = [
        d.strip().partition('=')[2].strip()
        for d in value.split(';')
        if d.strip().partition('=')[0].strip().lower() == 'max-age'
    ]
    if len(ages) != 1:
        return [f'{label}: Strict-Transport-Security needs exactly one max-age ({value!r})']
    digits = ages[0].strip('"')
    if not digits.isdigit():
        return [f'{label}: Strict-Transport-Security max-age is not a number ({value!r})']
    if int(digits) < MIN_HSTS_MAX_AGE:
        return [f'{label}: Strict-Transport-Security max-age={digits} < {MIN_HSTS_MAX_AGE}']
    return []


def check_frame_protection(resp: Response, label: str) -> list[str]:
    for value in resp.get_all('Content-Security-Policy'):
        ancestors = parse_csp(value).get('frame-ancestors')
        if ancestors and '*' not in ancestors:
            return []
    xfo = (resp.get('X-Frame-Options') or '').strip().upper()
    if xfo in ('DENY', 'SAMEORIGIN'):
        return []
    return [
        f'{label}: no clickjacking protection (CSP frame-ancestors without * or X-Frame-Options)'
    ]


def check_simple_headers(resp: Response, label: str) -> list[str]:
    found: list[str] = []
    if (resp.get('X-Content-Type-Options') or '').strip().lower() != 'nosniff':
        found.append(f'{label}: X-Content-Type-Options is not nosniff')
    referrer = resp.get('Referrer-Policy')
    if referrer is None:
        found.append(f'{label}: Referrer-Policy missing')
    else:
        tokens = [t.strip().lower() for t in referrer.split(',')]
        if UNSAFE_REFERRER_TOKENS & set(tokens):
            found.append(f'{label}: Referrer-Policy {referrer!r} leaks full URLs')
        elif not any(t and t in VALID_REFERRER_TOKENS for t in tokens):
            found.append(f'{label}: Referrer-Policy {referrer!r} has no valid token')
    permissions = resp.get('Permissions-Policy')
    if permissions is None or not permissions.strip():
        found.append(f'{label}: Permissions-Policy missing or empty')
    server = resp.get('Server')
    if (
        server is not None
        and SERVER_VERSION.search(server)
        and server.strip().lower() not in BENIGN_SERVER_VALUES
    ):
        found.append(f'{label}: Server header leaks a version ({server!r})')
    if resp.get('X-Powered-By') is not None:
        found.append(f'{label}: X-Powered-By header present')
    return found


def check_cookies(resp: Response, label: str, https: bool) -> list[str]:
    found: list[str] = []
    for raw in resp.get_all('Set-Cookie'):
        attrs = [a.strip() for a in raw.split(';')]
        name = attrs[0].split('=', 1)[0].strip()
        flags = {a.split('=', 1)[0].lower(): a.partition('=')[2].lower() for a in attrs[1:] if a}
        if https and 'secure' not in flags:
            found.append(f'{label}: cookie {name} lacks Secure')
        if name != CSRF_COOKIE and 'httponly' not in flags:
            found.append(f'{label}: cookie {name} lacks HttpOnly')
        if flags.get('samesite') not in ('lax', 'strict'):
            found.append(f'{label}: cookie {name} lacks SameSite=Lax|Strict')
    return found


def check_response(resp: Response, label: str, https: bool) -> list[str]:
    found: list[str] = []
    expected = EXPECTED_STATUS.get(label)
    if expected is not None and resp.status != expected:
        found.append(f'{label}: unexpected status {resp.status} (wanted {expected})')
    if https:
        found += check_hsts(resp, label)
    found += check_csp(resp, label)
    found += check_frame_protection(resp, label)
    found += check_simple_headers(resp, label)
    found += check_cookies(resp, label, https)
    return found


def check_redirect(resp: Response, label: str, host: str) -> list[str]:
    location = resp.get('Location') or ''
    try:
        target = urlsplit(location)
        target_host = target.hostname
    except ValueError:
        target, target_host = urlsplit(''), None
    if (
        resp.status in REDIRECT_STATUSES
        and target.scheme.lower() == 'https'
        and target_host is not None
        and target_host.lower() == host.lower()
    ):
        return []
    return [
        f'{label}: plain HTTP does not redirect to https on {host} '
        f'(status {resp.status}, {location!r})'
    ]


def check_tls(info: TlsInfo, now: float, label: str) -> list[str]:
    found: list[str] = []
    if info.version not in ('TLSv1.2', 'TLSv1.3'):
        found.append(f'{label}: TLS version {info.version!r} is below TLSv1.2')
    if info.legacy_tls:
        found.append(f'{label}: server accepts TLS 1.0/1.1 (must require TLS 1.2+)')
    if not info.verified:
        found.append(f'{label}: certificate does not verify ({info.verify_error})')
    elif info.not_after is None:
        found.append(f'{label}: certificate expiry unknown')
    elif info.not_after - now < MIN_CERT_DAYS * 86400:
        days = int((info.not_after - now) // 86400)
        found.append(f'{label}: certificate expires in {days} days (< {MIN_CERT_DAYS})')
    return found


def default_http_url(base: str) -> str | None:
    """The plain-HTTP twin of an https base, or None when the port is not the default."""
    try:
        parts = urlsplit(base)
        port, host = parts.port, parts.hostname
    except ValueError:
        return None
    if port not in (None, 443) or not host:
        return None
    return f'http://[{host}]/' if ':' in host else f'http://{host}/'


def _validate(base: str) -> tuple[str, int, bool]:
    """(host, port, https) of *base*, or TargetUnusable for anything that is not a usable URL."""
    try:
        parts = urlsplit(base)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise TargetUnusable(f'bad URL {base!r}: {exc}') from exc
    if parts.scheme not in ('http', 'https') or not host:
        raise TargetUnusable(f'not an http(s) URL: {base!r}')
    try:
        host.encode('idna')
    except UnicodeError as exc:
        raise TargetUnusable(f'bad host in {base!r}: {exc}') from exc
    https = parts.scheme == 'https'
    return host, port or (443 if https else 80), https


def run_checks(
    base: str,
    fetch: Fetcher,
    probe: TlsProbe,
    *,
    http_url: str | None = None,
    skip_redirect_check: bool = False,
    now: float | None = None,
    notes: list[str] | None = None,
) -> list[str]:
    """Run every check against *base*; return the findings. Raises TargetUnusable.

    *notes* collects what could not be verified; those lines are never findings and
    never a pass.
    """
    host, port, https = _validate(base)
    notes = notes if notes is not None else []
    root = base.rstrip('/')
    findings: list[str] = []
    saw_cookie = False
    for path in EXPECTED_STATUS:
        resp = fetch(root + path)
        saw_cookie = saw_cookie or bool(resp.get_all('Set-Cookie'))
        findings += check_response(resp, path, https)
    if not saw_cookie:
        notes.append('cookies: none observed (UNVERIFIED)')
    if https:
        info = probe(host, port)
        if info.legacy_tls is None:
            notes.append(
                'TLS 1.0/1.1 rejection UNVERIFIED (local OpenSSL cannot offer those versions)'
            )
        findings += check_tls(info, time.time() if now is None else now, 'tls')
        if not skip_redirect_check:
            target = http_url or default_http_url(base)
            if target is None:
                findings.append(
                    'redirect: base uses a non-default port; pass --http-url or '
                    '--skip-redirect-check'
                )
            else:
                findings += check_redirect(fetch(target), 'redirect', host)
    else:
        findings.append('base URL is not https (HSTS, TLS and redirect checks not applicable)')
    return findings


def _on_deadline(_signum: int, _frame: FrameType | None) -> None:
    raise TargetUnusable('overall deadline exceeded')


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or '').split('\n\n')[0])
    ap.add_argument('base_url', help='e.g. https://opentranscribe.example.com')
    ap.add_argument('--http-url', help='plain-HTTP URL expected to redirect to https')
    ap.add_argument('--skip-redirect-check', action='store_true')
    ap.add_argument(
        '--allow-http',
        action='store_true',
        help='accept an http:// base (throwaway stacks); header checks still run',
    )
    ap.add_argument(
        '--insecure',
        action='store_true',
        help='do not verify the certificate for header requests; the TLS check still reports it',
    )
    ap.add_argument(
        '--deadline',
        type=int,
        default=DEFAULT_DEADLINE_SECONDS,
        help='overall time budget in seconds before giving up with exit 2 (default 60)',
    )
    args = ap.parse_args(argv)

    notes: list[str] = []
    signal.signal(signal.SIGALRM, _on_deadline)
    signal.alarm(max(1, args.deadline))
    try:
        findings = run_checks(
            args.base_url,
            lambda url: http_fetch(url, args.insecure),
            tls_probe,
            http_url=args.http_url,
            skip_redirect_check=args.skip_redirect_check,
            notes=notes,
        )
    except (TargetUnusable, ValueError) as exc:
        print(f'UNUSABLE: {exc}', file=sys.stderr)
        return EXIT_UNUSABLE
    finally:
        signal.alarm(0)
    if args.allow_http:
        findings = [f for f in findings if not f.startswith('base URL is not https')]
    for note in notes:
        print(f'NOTE: {note}')
    for item in findings:
        print(f'FINDING: {item}')
    if findings:
        print(f'{len(findings)} finding(s) for {args.base_url}')
        return EXIT_FINDINGS
    print(f'OK: {args.base_url} passed all header and TLS checks (see NOTE lines for the rest)')
    return EXIT_PASS


if __name__ == '__main__':
    sys.exit(main())
