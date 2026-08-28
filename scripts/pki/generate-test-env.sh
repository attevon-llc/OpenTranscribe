#!/bin/bash
#
# Generate the PKI test-env fragment + compose overlay used to run PKI/mTLS
# testing (including `RUN_PKI_E2E=true pytest backend/tests/e2e/test_pki.py`)
# without ever reading, writing, or editing the tracked `.env` file.
#
# Emits two generated, gitignored artifacts into scripts/pki/test-certs/
# (already gitignored — see .gitignore:318 — and already where the certs this
# script references live):
#
#   pki-test.env           plain KEY=VALUE. Valid as BOTH a `docker compose
#                           env_file:` entry (via the generated overlay below)
#                           AND a `set -a; source` target for a shell/pytest run.
#                           One file, two consumers, so the two can never drift.
#
#   pki-test.compose.yml   appends that fragment to `backend`'s env_file list
#                           (env_file: lists APPEND across compose files and the
#                           LATER file wins — verified on Docker Compose
#                           v2.29.7) and mounts the CA cert. `.env` already sets
#                           PKI_ENABLED=false and an empty PKI_TRUSTED_PROXIES
#                           (fail-closed), so this overlay is what turns PKI on
#                           for a test run without touching that file at all.
#
# This script itself never opens `.env` — not to read it, not to grep it, not
# to check whether a key is set in it. If a value the caller cares about is
# already exported in the environment (e.g. by opentr.sh, which sources .env
# before calling this), that value is the base the flags below override;
# otherwise a coded default is used.
#
# Usage: ./scripts/pki/generate-test-env.sh [options]
#
#   --https-port N       Host port for the mTLS listener   (default: $PKI_HTTPS_PORT or 5182)
#   --http-port N        Host port for the plain listener  (default: $PKI_HTTP_PORT  or 5187)
#   --admin-cert NAME     Client cert whose DN gets admin  (default: pkiadmin; repeatable)
#   --trusted-proxies CIDR[,CIDR...]  (default: 127.0.0.1/32,172.16.0.0/12,192.168.0.0/16)
#   --verify-revocation   Turn OCSP/CRL checking on        (default: off)
#   --force-certs         Re-issue client certs even if present (rotates keys —
#                          invalidates any browser-imported .p12)
#   --print                Print the resolved values and exit; write nothing
#   --quiet                Machine mode: suppress the human-readable banner
#   -h, --help              Show this help and exit
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PKI_DIR="${SCRIPT_DIR}/test-certs"

usage() {
  sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

HTTPS_PORT="${PKI_HTTPS_PORT:-5182}"
HTTP_PORT="${PKI_HTTP_PORT:-5187}"
ADMIN_CERTS=()
# 172.16.0.0/12 alone is not Docker's whole auto-assigned bridge-network range:
# once its default pools (172.17.0.0/16-172.31.0.0/16) are exhausted by other
# concurrent Docker networks on the host, the daemon spills into 192.168.0.0/16
# chunks (issue #615) -- measured live on a host running ~34 unrelated Docker
# networks, where even the ORDINARY non-fresh `opentranscribe_default` network
# (not a --fresh deployment) landed at 192.168.96.0/20. Both ranges are private
# RFC1918 space Docker itself hands out for its own bridge networks, never
# attacker-reachable from outside the host, so widening to cover both does not
# change the threat model this allowlist defends against (a header injected by
# something outside our own docker network) -- it just stops the allowlist
# silently missing the range Docker actually used.
TRUSTED_PROXIES="127.0.0.1/32,172.16.0.0/12,192.168.0.0/16"
VERIFY_REVOCATION="false"
FORCE_CERTS=""
PRINT_ONLY=""
QUIET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --https-port)
      HTTPS_PORT="$2"
      shift 2
      ;;
    --http-port)
      HTTP_PORT="$2"
      shift 2
      ;;
    --admin-cert)
      ADMIN_CERTS+=("$2")
      shift 2
      ;;
    --trusted-proxies)
      TRUSTED_PROXIES="$2"
      shift 2
      ;;
    --verify-revocation)
      VERIFY_REVOCATION="true"
      shift
      ;;
    --force-certs)
      FORCE_CERTS="1"
      shift
      ;;
    --print)
      PRINT_ONLY="1"
      shift
      ;;
    --quiet)
      QUIET="1"
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "❌ Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [ "${#ADMIN_CERTS[@]}" -eq 0 ]; then
  ADMIN_CERTS=("pkiadmin")
fi

log() {
  [ -n "$QUIET" ] && return 0
  echo "$@"
}

cd "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# 1. Certificates. Reuse by default — setup-test-pki.sh regenerates every
#    CLIENT cert unconditionally each time it runs (only the CA key is
#    guarded), so calling it on every invocation would rotate keys and
#    invalidate any browser-imported .p12. The idempotency guard lives here.
# ---------------------------------------------------------------------------
need_setup=""
if [ -n "$FORCE_CERTS" ] || [ ! -f "${PKI_DIR}/ca/ca.crt" ]; then
  need_setup="1"
else
  for cert in "${ADMIN_CERTS[@]}"; do
    [ -f "${PKI_DIR}/clients/${cert}.crt" ] || need_setup="1"
  done
fi

if [ -n "$need_setup" ]; then
  if [ -n "$PRINT_ONLY" ]; then
    echo "(--print) would run ./scripts/pki/setup-test-pki.sh to generate certificates"
    exit 0
  fi
  log "🔐 Generating PKI test certificates (scripts/pki/setup-test-pki.sh)..."
  "${SCRIPT_DIR}/setup-test-pki.sh" >/dev/null
else
  log "🔐 Reusing existing PKI test certificates (pass --force-certs to rotate)."
fi

# ---------------------------------------------------------------------------
# 2. Nginx mTLS server certificate. Moved here from opentr.sh, which used to
#    inline this exact openssl block in TWO places (start_app + reset) — one
#    copy now, called by both.
# ---------------------------------------------------------------------------
mkdir -p "${PKI_DIR}/nginx"
if [ -n "$FORCE_CERTS" ] || [ ! -f "${PKI_DIR}/nginx/server.crt" ] || [ ! -f "${PKI_DIR}/nginx/server.key" ]; then
  if [ -n "$PRINT_ONLY" ]; then
    echo "(--print) would generate ${PKI_DIR}/nginx/server.{crt,key}"
    exit 0
  fi
  log "🔐 Generating PKI nginx server certificate..."
  openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout "${PKI_DIR}/nginx/server.key" -out "${PKI_DIR}/nginx/server.crt" \
    -subj "/CN=${PKI_SERVER_NAME:-localhost}" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
  # openssl's -keyout honours the process umask, which left server.key at 0600
  # on this host — unreadable by the frontend container's non-root nginx user
  # (Dockerfile.prod runs it unprivileged), so nginx failed to start with
  # "cannot load certificate key ... Permission denied" the first time
  # --with-pki actually ran in dev (it was previously unreachable — see
  # opentr.sh's add_pki_overlay). setup-test-pki.sh's own client/CA certs are
  # already 644 for the same bind-mount reason; match that here.
  chmod 644 "${PKI_DIR}/nginx/server.key" "${PKI_DIR}/nginx/server.crt"
else
  log "🔐 Reusing existing PKI nginx server certificate."
fi

# ---------------------------------------------------------------------------
# 3. Derive PKI_ADMIN_DNS from the cert(s) — never hardcode it. RFC2253 is the
#    format nginx's $ssl_client_s_dn emits (nginx >= 1.11.6), which is what
#    pki_auth._normalize_dn compares against. Multiple admin certs join with
#    ';' — PKI_ADMIN_DNS is semicolon-delimited (backend/app/auth/ldap_auth.py
#    _parse_group_list), NOT comma-delimited: a DN itself contains commas.
# ---------------------------------------------------------------------------
admin_dns=()
for cert in "${ADMIN_CERTS[@]}"; do
  crt="${PKI_DIR}/clients/${cert}.crt"
  if [ ! -f "$crt" ]; then
    echo "❌ Admin cert not found: $crt (expected setup-test-pki.sh to create '${cert}')" >&2
    exit 1
  fi
  dn="$(openssl x509 -in "$crt" -noout -subject -nameopt RFC2253 | sed 's/^subject=//')"
  admin_dns+=("$dn")
done
IFS=';'
ADMIN_DN_JOINED="${admin_dns[*]}"
unset IFS

PKI_E2E_URL="https://localhost:${HTTPS_PORT}"

if [ -n "$PRINT_ONLY" ]; then
  echo "PKI_ENABLED=true"
  echo "PKI_TRUSTED_PROXIES=${TRUSTED_PROXIES}"
  echo "PKI_ADMIN_DNS=${ADMIN_DN_JOINED}"
  echo "PKI_VERIFY_REVOCATION=${VERIFY_REVOCATION}"
  echo "PKI_HTTPS_PORT=${HTTPS_PORT}"
  echo "PKI_HTTP_PORT=${HTTP_PORT}"
  echo "PKI_E2E_URL=${PKI_E2E_URL}"
  echo ""
  echo "# would write:"
  echo "#   ${PKI_DIR}/pki-test.env"
  echo "#   ${PKI_DIR}/pki-test.compose.yml"
  exit 0
fi

# ---------------------------------------------------------------------------
# 4. Write the fragment. Deterministic given the same inputs (no timestamps,
#    no PIDs) so repeated runs are byte-identical — that is what makes reuse
#    (step 1) observable rather than merely assumed.
#
# Every value is single-quoted. A subject DN routinely contains spaces
# ("CN=PKI Admin User") and commas — unquoted, `set -a; source` word-splits
# the line and tries to run the second word as a command (observed:
# "line 15: Admin: command not found"). Single quotes are safe for BOTH
# consumers: bash `source` performs no expansion inside them (unlike double
# quotes, which still interpolate `$`), and docker compose's env_file parser
# strips a single-quote pair the same way it strips double quotes (verified
# on Compose v2.29.7 — `KEY='a, b'` resolves to the unquoted value in both).
# A value containing a literal single quote would need real escaping neither
# parser's simple format shares, so that case is refused rather than silently
# mishandled — see _env_kv below.
# ---------------------------------------------------------------------------
ENV_FILE="${PKI_DIR}/pki-test.env"

_env_kv() {
  local key="$1" value="$2"
  case "$value" in
    *"'"*)
      echo "❌ ${key} contains a single quote, which the generated fragment cannot safely" >&2
      echo "   quote for both docker compose AND 'set -a; source'. Value: ${value}" >&2
      exit 1
      ;;
  esac
  printf "%s='%s'\n" "$key" "$value"
}

{
  echo "# AUTO-GENERATED by scripts/pki/generate-test-env.sh — do NOT edit, do NOT commit."
  echo "# Regenerate: ./scripts/pki/generate-test-env.sh"
  echo "# This file exists so PKI testing never requires editing .env."
  echo "#"
  echo "# One file, two consumers: valid as a \`docker compose env_file:\` entry (via"
  echo "# the generated pki-test.compose.yml) AND as a \`set -a; source\` target for a"
  echo "# shell/pytest run — see scripts/pki/generate-test-env.sh's header. Every value"
  echo "# is single-quoted; both consumers strip/honour that identically (see above)."
  echo ""
  echo "# --- container-side (reaches backend via the generated compose overlay) ---"
  _env_kv PKI_ENABLED "true"
  _env_kv PKI_TRUSTED_PROXIES "$TRUSTED_PROXIES"
  _env_kv RATE_LIMIT_TRUSTED_PROXIES "$TRUSTED_PROXIES"
  _env_kv PKI_CERT_HEADER "X-Client-Cert"
  _env_kv PKI_CERT_DN_HEADER "X-Client-Cert-DN"
  _env_kv PKI_ADMIN_DNS "$ADMIN_DN_JOINED"
  _env_kv PKI_VERIFY_REVOCATION "$VERIFY_REVOCATION"
  _env_kv PKI_CA_CERT_PATH "/etc/opentranscribe/pki/ca.crt"
  echo ""
  echo "# --- host-side (compose interpolation + pytest) ---"
  _env_kv PKI_HTTPS_PORT "$HTTPS_PORT"
  _env_kv PKI_HTTP_PORT "$HTTP_PORT"
  _env_kv PKI_E2E_URL "$PKI_E2E_URL"
  _env_kv RUN_PKI_E2E "true"
} > "$ENV_FILE"

# ---------------------------------------------------------------------------
# 5. Write the compose overlay. Paths are project-root-relative: compose
#    resolves BOTH env_file and short-syntax volume sources against the
#    PROJECT directory, not this generated file's directory (verified on
#    Docker Compose v2.29.7 with a fragment declared from a subdirectory).
# ---------------------------------------------------------------------------
COMPOSE_FILE="${PKI_DIR}/pki-test.compose.yml"
cat > "$COMPOSE_FILE" <<'EOF'
# AUTO-GENERATED by scripts/pki/generate-test-env.sh. Safe to delete; regenerated
# on demand. Do NOT edit by hand, do NOT commit.
#
# env_file lists APPEND across compose files and the LATER file wins, so this
# overrides the base `env_file: .env` without reading or modifying it. Paths
# are project-root-relative: verified, compose resolves both env_file and
# short-syntax volume sources against the project directory, not this file's.
services:
  backend:
    env_file:
      - ./scripts/pki/test-certs/pki-test.env
    volumes:
      - ./scripts/pki/test-certs/ca/ca.crt:/etc/opentranscribe/pki/ca.crt:ro
EOF

log ""
log "🔐 PKI test env ready:"
log "   Fragment: ${ENV_FILE}"
log "   Overlay:  ${COMPOSE_FILE}"
log ""
log "   Access URL:  ${PKI_E2E_URL}"
log "   Admin DN(s): ${ADMIN_DN_JOINED}"
log "   Client certs: ${PKI_DIR}/clients/*.p12 (password: changeit)"
log ""
log "   Run the E2E suite against this fragment with:"
log "     set -a; source ${ENV_FILE}; set +a"
log "     RUN_PKI_E2E=true pytest backend/tests/e2e/test_pki.py -v"
log ""
log "   .env was never opened, read, or written by this script."
