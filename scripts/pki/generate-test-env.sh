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
#   --trusted-proxies CIDR[,CIDR...]  (default: derived from this compose project's own
#                          docker network -- see --print-trusted-proxies)
#   --print-trusted-proxies  Resolve the allowlist, print it, exit; touch nothing else
#   --backend-bind-host HOST  Host interface the backend's published port binds to
#                          (default: 127.0.0.1 — the LAN must not reach the backend
#                          directly when a DN header is trusted from the proxy net)
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

# Print the whole leading comment block, however long it grows. This used to be
# `sed -n '2,32p'`, a line range that had already fallen behind the option list
# it was meant to print: `--help` stopped at `--http-port` and never mentioned
# --admin-cert, --trusted-proxies, --print or --quiet at all.
usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
}

HTTPS_PORT="${PKI_HTTPS_PORT:-5182}"
HTTP_PORT="${PKI_HTTP_PORT:-5187}"
ADMIN_CERTS=()

# ---------------------------------------------------------------------------
# Trusted-proxy allowlist (issue #620).
#
# WHAT THIS VALUE ACTUALLY GATES. It is written out as BOTH
# RATE_LIMIT_TRUSTED_PROXIES *and* PKI_TRUSTED_PROXIES, and those two are not
# the same kind of setting:
#
#   RATE_LIMIT_TRUSTED_PROXIES decides whose X-Forwarded-For is believed for
#     per-IP rate limiting / lockout / the audit trail's client IP. A too-wide
#     value costs attribution.
#   PKI_TRUSTED_PROXIES decides whether a bare `X-Client-Cert-DN` header is
#     believed AS AN IDENTITY. `backend/app/auth/pki_auth.py`'s
#     `_extract_user_info_from_request` accepts a DN with NO certificate at all
#     whenever `header_trust.header_source_is_trusted()` is true, and
#     `pki_mode` defaults to "header", so DN-only IS the default transport. A
#     too-wide value is unauthenticated admin impersonation: the admin DN is
#     not a secret (setup-test-pki.sh hardcodes it and `pkiadmin` is this
#     script's own default --admin-cert).
#
# The previous default was `127.0.0.1/32,172.16.0.0/12,192.168.0.0/16`, and its
# comment justified the /16 on two claims that were both wrong:
#
#   * "it only enables X-Forwarded-For spoofing / lockout evasion" — no, it
#     also gates identity assertion, per the paragraph above.
#   * "production never loads it" — `opentr.sh`'s add_pki_overlay() generates
#     and sources this fragment for `./opentr.sh start prod --build --with-pki`
#     too, which is a documented command in the root CLAUDE.md.
#
# 192.168.0.0/16 is the range ordinary consumer/office routers hand out, and
# docker-compose.yml publishes the backend on the host's wildcard address, so
# on any such LAN every other device could POST a forged DN header straight to
# the backend and be handed admin tokens. Two changes close that: the backend's
# published port is now bound to loopback for a --with-pki stack (see
# BACKEND_BIND_HOST below), and this allowlist is DERIVED rather than guessed.
#
# WHY DERIVED. #615 is real: once Docker's default pools
# (172.17.0.0/16-172.31.0.0/16) are exhausted by other networks on the host,
# the daemon spills into 192.168.0.0/16 in /20 chunks -- measured live, where
# even the ordinary non-fresh `opentranscribe_default` network landed on
# 192.168.96.0/20. Asking Docker which subnet it actually used covers that case
# exactly, without trusting the 4095 other /20s in the same /16. It is also
# safe by construction: Docker's IPAM refuses a pool that overlaps an existing
# host route, so a subnet it allocated cannot be the LAN this host sits on.
#
# FALLBACK. When the daemon can't be reached, or the project's network does not
# exist yet (a first-ever `--with-pki` start creates it only during `up`), we
# fall back to Docker's default pool and say so loudly. On a host crowded
# enough for #615's spill, that first start can still fail the fail-closed
# trust check; the second one works, because the network exists by then. Pass
# --trusted-proxies to skip the guessing entirely.
PKI_TRUSTED_PROXIES_FALLBACK="127.0.0.1/32,172.16.0.0/12"
TRUSTED_PROXIES=""
TRUSTED_PROXIES_EXPLICIT=""
TRUSTED_PROXIES_SOURCE=""
PRINT_TRUSTED_ONLY=""

# The backend's published port binds here for a --with-pki stack. Loopback is
# the point: with mTLS terminated by the PKI nginx, nothing on the LAN has any
# business reaching the backend's own port, and reaching it directly is what
# turned a wide trusted-proxy CIDR into remote admin impersonation.
#
# Deliberately NOT `${BACKEND_BIND_HOST:-127.0.0.1}`. opentr.sh sources .env
# before calling this script, and .env.example carries a live
# BACKEND_BIND_HOST line for ordinary deployments — reading the ambient value
# would let a stock .env silently switch the control back off, the same trap
# add_pki_overlay documents for PKI_HTTP_PORT. Widening it takes the explicit
# --backend-bind-host flag, which is then printed in the banner.
BACKEND_BIND_HOST="127.0.0.1"

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
      TRUSTED_PROXIES_EXPLICIT="$2"
      shift 2
      ;;
    --print-trusted-proxies)
      PRINT_TRUSTED_ONLY="1"
      shift
      ;;
    --backend-bind-host)
      BACKEND_BIND_HOST="$2"
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

# ---------------------------------------------------------------------------
# 0. Resolve the trusted-proxy allowlist. See the long note beside
#    PKI_TRUSTED_PROXIES_FALLBACK for why this is derived and not a constant.
# ---------------------------------------------------------------------------

# How docker compose names a project when COMPOSE_PROJECT_NAME is unset: the
# project directory's basename, lowercased, with everything outside
# [a-z0-9_-] dropped.
compose_project_default() {
  basename "$PROJECT_DIR" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-'
}

# Subnets docker actually allocated to THIS project's default network.
# Prints a comma-separated list, or nothing when the daemon or the network is
# unavailable. Never falls back to "every docker network on the host": most of
# them belong to unrelated projects and trusting those would be no narrower
# than the /16 this replaces.
derive_docker_subnets() {
  command -v docker > /dev/null 2>&1 || return 0

  local candidates=() name out subnet seen="" all=""
  if [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then
    # Set (by .env, or by opentr.sh for a --fresh deployment) means the name is
    # not a guess: compose uses it, and docker-compose.nginx.yml pins the
    # network to the same ${COMPOSE_PROJECT_NAME}_default. One candidate.
    candidates+=("${COMPOSE_PROJECT_NAME}_default")
  else
    # Unset, so the name depends on which overlays load: compose derives it
    # from the directory, while docker-compose.nginx.yml pins the literal
    # `opentranscribe_default`. Both are candidates, and both get collected
    # rather than stopping at the first hit — a directory-named network left
    # behind by an unrelated project would otherwise shadow the real stack's
    # and reintroduce #615's silent trust failure.
    candidates+=("$(compose_project_default)_default" "opentranscribe_default")
  fi

  for name in "${candidates[@]}"; do
    case ",${seen}," in *",${name},"*) continue ;; esac
    seen="${seen},${name}"
    out=""
    out="$(docker network inspect "$name" \
      --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2> /dev/null)" || continue
    for subnet in $out; do
      case ",${all}," in *",${subnet},"*) continue ;; esac
      all="${all:+${all},}${subnet}"
    done
  done
  printf '%s' "$all"
}

resolve_trusted_proxies() {
  if [ -n "$TRUSTED_PROXIES_EXPLICIT" ]; then
    TRUSTED_PROXIES="$TRUSTED_PROXIES_EXPLICIT"
    TRUSTED_PROXIES_SOURCE="--trusted-proxies"
    return 0
  fi

  local derived
  derived="$(derive_docker_subnets)"
  if [ -n "$derived" ]; then
    # Loopback stays in the list because a host-side caller that reaches the
    # published port over 127.0.0.1 may arrive either as the bridge gateway
    # (inside the derived subnet) or as loopback itself, depending on whether
    # docker's userland proxy handled the connection.
    TRUSTED_PROXIES="127.0.0.1/32,${derived}"
    TRUSTED_PROXIES_SOURCE="docker network"
    return 0
  fi

  TRUSTED_PROXIES="$PKI_TRUSTED_PROXIES_FALLBACK"
  TRUSTED_PROXIES_SOURCE="fallback"
}

resolve_trusted_proxies

if [ -n "$PRINT_TRUSTED_ONLY" ]; then
  echo "$TRUSTED_PROXIES"
  exit 0
fi

if [ "$TRUSTED_PROXIES_SOURCE" = "fallback" ]; then
  echo "⚠️  Could not read this project's docker network, so PKI_TRUSTED_PROXIES falls back" >&2
  echo "    to ${PKI_TRUSTED_PROXIES_FALLBACK} (docker's default bridge pool)." >&2
  echo "    If PKI sign-in fails silently on this start, the daemon spilled outside that" >&2
  echo "    pool (issue #615): start once more so the network exists, or pass" >&2
  echo "    --trusted-proxies <cidr>." >&2
fi

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
  echo "BACKEND_BIND_HOST=${BACKEND_BIND_HOST}"
  echo ""
  echo "# PKI_TRUSTED_PROXIES source: ${TRUSTED_PROXIES_SOURCE}"
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
  echo "# docker-compose.yml publishes the backend at"
  echo "# \${BACKEND_BIND_HOST:-0.0.0.0}:\${BACKEND_PORT}. opentr.sh sources this"
  echo "# fragment with 'set -a' BEFORE assembling the compose chain, so this line is"
  echo "# what keeps a --with-pki backend off the LAN: mTLS-terminating nginx is the"
  echo "# only front door, and a DN header from a LAN peer can no longer reach the"
  echo "# backend's own port at all (issue #620)."
  _env_kv BACKEND_BIND_HOST "$BACKEND_BIND_HOST"
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
log "   Trusted proxies: ${TRUSTED_PROXIES}  (${TRUSTED_PROXIES_SOURCE})"
log "   Backend port bound to: ${BACKEND_BIND_HOST}"
log ""
log "   Run the E2E suite against this fragment with:"
log "     set -a; source ${ENV_FILE}; set +a"
log "     RUN_PKI_E2E=true pytest backend/tests/e2e/test_pki.py -v"
log ""
log "   .env was never opened, read, or written by this script."
