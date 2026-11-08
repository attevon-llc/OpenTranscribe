#!/usr/bin/env bash
# test-lan-install.sh - check an OpenTranscribe LAN install FROM A CLIENT DEVICE (issue #1264).
#
# Run this on a machine that is NOT the server (a laptop on the same network; it needs bash,
# curl and openssl). It is read-only: it never signs in, uploads, or changes anything. The only
# request that touches the app is GET /api/auth/session, which answers 200 to anonymous visitors.
#
# What it proves from the client's side of the wire:
#   1  https://<host> answers, and the certificate chains to the local CA you give it (--ca)
#   2  the certificate covers the address you used (hostname or IP)
#   3  the app answers over HTTPS (/api/auth/session -> 200)
#   4  plain HTTP on 80 only redirects to HTTPS (never serves the app)
#   5  the plain app port (5173) is NOT reachable from this device (D11 / #1332)
#   6  the API (8080) and docs (5183) ports are NOT reachable from this device
#   7  the sign-in cookie is flagged Secure (browsers drop it otherwise)
#   8  (optional) without --ca, the certificate is rejected: proves the CA is not already trusted
#      system-wide by accident (--expect-untrusted)
#
# What it CANNOT prove (do these by hand on each phone/laptop, see the checklist it prints):
# that the OS/browser trust store accepted ca.crt, and that the microphone prompt appears.
#
# Usage:
#   scripts/test-lan-install.sh --host opentranscribe.local --ca ./ca.crt
#   scripts/test-lan-install.sh --host 192.168.1.50 --ca ./ca.crt --https-port 8443
#
# Exit: 0 all checks passed, 1 a check failed, 2 bad usage. Not wired into CI or release.sh:
# it needs a real server and a real client on a LAN.
set -uo pipefail

HOST=""
CA=""
HTTPS_PORT=443
HTTP_PORT=80
PLAIN_APP_PORT=5173
API_PORT=8080
DOCS_PORT=5183
EXPECT_UNTRUSTED=0

usage() {
  sed -n '2,/^set -uo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

# --- pure classifiers (unit-tested: backend/tests/unit/test_lan_harness_script.py) ---------

# curl exit codes that mean "nothing answered": 7 refused, 28 timeout, 52 empty reply, 56 reset.
port_rc_is_closed() {
  case "$1" in 7 | 28 | 52 | 56) return 0 ;; *) return 1 ;; esac
}

# "<http_code> <redirect_url>" from curl: a redirect to https only.
redirect_is_https() {
  case "$1" in 30[1278]\ https://*) return 0 ;; *) return 1 ;; esac
}

# Does a `subjectAltName` extension dump name <host> as DNS or IP?
san_covers_host() {
  local host="$1" sans="$2" item
  while IFS= read -r item || [ -n "$item" ]; do
    item="${item# }"
    case "$item" in "DNS:${host}" | "IP Address:${host}") return 0 ;; esac
  done < <(printf '%s' "$sans" | tr ',' '\n' | sed 's/^ *//')
  return 1
}

# stdin: Set-Cookie header lines. Passes when there are none or every one has Secure.
cookies_are_secure() {
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    printf '%s' "$line" | grep -qi ';[[:space:]]*secure' || return 1
  done
  return 0
}

is_local_host() {
  case "$1" in 127.* | localhost | ::1) return 0 ;; *) return 1 ;; esac
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --host) HOST="${2:-}"; shift 2 ;;
      --ca) CA="${2:-}"; shift 2 ;;
      --https-port) HTTPS_PORT="${2:-}"; shift 2 ;;
      --http-port) HTTP_PORT="${2:-}"; shift 2 ;;
      --plain-app-port) PLAIN_APP_PORT="${2:-}"; shift 2 ;;
      --expect-untrusted) EXPECT_UNTRUSTED=1; shift ;;
      -h | --help) usage; return 3 ;;
      *) echo "unknown argument: $1" >&2; return 2 ;;
    esac
  done
  [ -n "$HOST" ] || { echo "--host is required" >&2; return 2; }
  if [ -z "$CA" ] || [ ! -f "$CA" ]; then
    echo "--ca must point at the server's nginx/ssl/ca.crt" >&2
    return 2
  fi
  if is_local_host "$HOST"; then
    echo "Run this from another device: on the server itself every port is reachable, so the closed-port checks are meaningless." >&2
    return 2
  fi
  return 0
}

port_closed() {
  curl -sS --max-time 4 -o /dev/null "http://${HOST}:${1}/" 2>/dev/null
  port_rc_is_closed $?
}

[ "${OT_LAN_SOURCE_ONLY:-}" = 1 ] && return 0 2>/dev/null

parse_args "$@"
rc=$?
case "$rc" in 0) ;; 3) exit 0 ;; *) exit "$rc" ;; esac
for tool in curl openssl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "$tool is required" >&2; exit 2; }
done

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n        %s\n' "$1" "${2:-}"; }

BASE="https://${HOST}:${HTTPS_PORT}"
[ "$HTTPS_PORT" = 443 ] && BASE="https://${HOST}"

echo "Checking ${BASE} from this device"

# 1 + 3: trusted TLS and the app answering.
body="$(curl -sS --max-time 10 --cacert "$CA" -o /dev/null -w '%{http_code}' "${BASE}/api/auth/session" 2>&1)"
if [ "$body" = "200" ]; then
  ok "1/3 HTTPS answers and the certificate chains to ca.crt; /api/auth/session returned 200"
else
  bad "1/3 HTTPS with ca.crt" "got '${body}'. Wrong --ca, host not covered by the certificate, or the stack is down."
fi

# 2: the certificate's subject names include the address used.
sans="$(openssl s_client -connect "${HOST}:${HTTPS_PORT}" -servername "$HOST" </dev/null 2>/dev/null |
  openssl x509 -noout -ext subjectAltName 2>/dev/null)"
if san_covers_host "$HOST" "$sans"; then
  ok "2 certificate covers ${HOST}"
else
  bad "2 certificate covers ${HOST}" "subjectAltName was: ${sans:-unreadable}. Re-run scripts/generate-ssl-cert.sh with this name/IP (--auto-ip)."
fi

# 4: plain HTTP only redirects.
code="$(curl -sS --max-time 6 -o /dev/null -w '%{http_code} %{redirect_url}' "http://${HOST}:${HTTP_PORT}/" 2>&1)"
if redirect_is_https "$code"; then ok "4 http://${HOST}:${HTTP_PORT} redirects to HTTPS"
else bad "4 plain HTTP redirects to HTTPS" "got '${code}'"; fi

# 5 + 6: ports that must not be exposed to the LAN.
if port_closed "$PLAIN_APP_PORT"; then ok "5 plain app port ${PLAIN_APP_PORT} is not reachable from here"
else bad "5 plain app port ${PLAIN_APP_PORT} reachable" "FRONTEND_BIND_HOST should be 127.0.0.1 behind nginx (issue #1332). Passwords sent there are unencrypted."; fi
for p in "$API_PORT" "$DOCS_PORT"; do
  if port_closed "$p"; then ok "6 port ${p} is not reachable from here"
  else bad "6 port ${p} reachable" "backend/docs must bind 127.0.0.1 in production (issue #1332)."; fi
done

# 7: Secure cookie. The session probe sets no cookie to anonymous visitors, so look at the
# login page's response headers for any Set-Cookie and require Secure on each one.
cookies="$(curl -sS --max-time 10 --cacert "$CA" -D - -o /dev/null "${BASE}/" 2>/dev/null | grep -i '^set-cookie:' || true)"
if [ -z "$cookies" ]; then
  ok "7 no cookie set to anonymous visitors (nothing to be insecure); confirm Secure at sign-in in the browser checklist"
elif printf '%s\n' "$cookies" | cookies_are_secure; then
  ok "7 every cookie set carries Secure"
else
  bad "7 cookies carry Secure" "$(printf '%s' "$cookies" | grep -vi secure | head -3)"
fi

# 8: optional negative control.
if [ "$EXPECT_UNTRUSTED" -eq 1 ]; then
  rc=0
  curl -sS --max-time 10 -o /dev/null "${BASE}/api/auth/session" 2>/dev/null || rc=$?
  if [ "$rc" -eq 60 ] || [ "$rc" -eq 35 ]; then ok "8 without ca.crt the certificate is rejected (CA is not trusted system-wide here)"
  else bad "8 negative control" "curl succeeded or failed differently (rc=${rc}): this device already trusts the CA, so check 1 proves less."; fi
fi

echo
echo "Result: ${PASS} passed, ${FAIL} failed"
cat <<EOF

By hand on each device (the script cannot see a phone's trust store):
  [ ] Install ca.crt (docs: installation/small-business-lan, Step 4) and enable full trust
      (iOS: Settings > General > About > Certificate Trust Settings).
  [ ] Open ${BASE} in the browser: no certificate warning, padlock shown.
  [ ] Sign in with a throwaway account; you stay signed in after a page reload.
  [ ] Open Record: the browser asks for microphone permission (only offered on HTTPS).
  [ ] Open https://<server-ip> as well as the hostname if both are meant to work.
EOF

[ "$FAIL" -eq 0 ]
