#!/bin/bash
#
# Issue a TLS SERVER certificate from a test CA kept beside the repo's test PKI
# (scripts/pki/test-certs/servers/ca), so a test container can serve TLS that the
# backend VERIFIES (not skips) once the CA file is mounted into it. Used by
# `./opentr.sh start ... --with-ldap-test` (LLDAPS), and usable for any other test
# service that needs a certificate the app can verify.
#
# Why a second CA instead of the one setup-test-pki.sh makes (test-certs/ca): that CA is
# for client (mTLS) certificates and carries no keyUsage extension. Python 3.13's
# ssl.create_default_context turns on VERIFY_X509_STRICT, and OpenSSL's strict mode
# rejects such a CA ("CA cert does not include key usage extension"), so the backend
# image would refuse a leaf signed by it. Re-signing that CA in place would touch the
# material a running --with-pki stack already trusts; a separate server CA does not.
#
# Usage:
#   ./scripts/pki/issue-test-server-cert.sh <name> <SAN> [<SAN>...] [--force]
#
#   <name>  output directory name under test-certs/servers/
#   <SAN>   subjectAltName entries, each "DNS:host" or "IP:address"
#
# Outputs (test-certs/servers/<name>/): server.crt, server.key, ca.crt
# Idempotent: an existing certificate is kept while it chains to the server CA, has
# more than 30 days left and carries every requested SAN; otherwise it is reissued.
#
# Test material only: the leaf key is world-readable (0644) because a container's non-root
# user must read a bind-mounted file, and the directory is gitignored.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKI_DIR="${SCRIPT_DIR}/test-certs"
CA_DIR="${PKI_DIR}/servers/ca"
CA_DAYS=3650
CERT_DAYS=365
RENEW_WITHIN_SECONDS=$((30 * 24 * 3600))

force=false
positional=()
for arg in "$@"; do
  case "$arg" in
    --force) force=true ;;
    -h | --help)
      sed -n '2,/^set -/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) positional+=("$arg") ;;
  esac
done

if [ "${#positional[@]}" -lt 2 ]; then
  echo "usage: $0 <name> <DNS:host|IP:addr>... [--force]" >&2
  exit 2
fi
name="${positional[0]}"
sans=("${positional[@]:1}")

if ! [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "error: <name> must match [A-Za-z0-9._-]+, got '$name'" >&2
  exit 2
fi
if [ "$name" = "ca" ]; then
  echo "error: <name> 'ca' is reserved for the server CA directory" >&2
  exit 2
fi
for san in "${sans[@]}"; do
  if ! [[ "$san" =~ ^(DNS|IP):[^[:space:],]+$ ]]; then
    echo "error: SAN must be DNS:<host> or IP:<address>, got '$san'" >&2
    exit 2
  fi
done

if [ ! -f "${CA_DIR}/ca.crt" ] || [ ! -f "${CA_DIR}/ca.key" ]; then
  echo "Test server CA missing; creating it" >&2
  mkdir -p "$CA_DIR"
  openssl req -x509 -newkey rsa:4096 -nodes -days "$CA_DAYS" \
    -keyout "${CA_DIR}/ca.key" -out "${CA_DIR}/ca.crt" \
    -subj "/C=US/ST=Virginia/L=Arlington/O=OpenTranscribe Test CA/OU=PKI Testing/CN=OpenTranscribe Test Server CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" 2>/dev/null
  chmod 644 "${CA_DIR}/ca.crt"
  chmod 600 "${CA_DIR}/ca.key"
fi

out="${PKI_DIR}/servers/${name}"
crt="${out}/server.crt"
key="${out}/server.key"

# True when the existing certificate can be kept as it is.
reusable() {
  [ -f "$crt" ] && [ -f "$key" ] || return 1
  openssl verify -CAfile "${CA_DIR}/ca.crt" "$crt" >/dev/null 2>&1 || return 1
  openssl x509 -in "$crt" -noout -checkend "$RENEW_WITHIN_SECONDS" >/dev/null 2>&1 || return 1
  local san value result
  for san in "${sans[@]}"; do
    value="${san#*:}"
    # Capture, then match: `openssl ... | grep -q` under pipefail can report a miss for a match.
    case "$san" in
      DNS:*) result="$(openssl x509 -in "$crt" -noout -checkhost "$value" 2>/dev/null)" || return 1 ;;
      IP:*) result="$(openssl x509 -in "$crt" -noout -checkip "$value" 2>/dev/null)" || return 1 ;;
      *) continue ;;
    esac
    [[ "$result" == *"does match"* ]] || return 1
  done
}

if [ "$force" = false ] && reusable; then
  echo "Reusing ${crt}" >&2
else
  mkdir -p "$out"
  csr="${out}/server.csr"
  ext="${out}/server.ext"
  san_list="$(IFS=,; echo "${sans[*]}")"
  first_dns="$(printf '%s\n' "${sans[@]}" | sed -n 's/^DNS://p' | head -1)"

  openssl genrsa -out "$key" 2048 2>/dev/null
  openssl req -new -key "$key" -out "$csr" \
    -subj "/C=US/ST=Virginia/L=Arlington/O=OpenTranscribe Test/OU=Test Servers/CN=${first_dns:-test-server}"
  # authorityKeyIdentifier and a serverAuth EKU are what strict X.509 verification
  # (Python 3.13's ssl default) requires of a leaf.
  cat >"$ext" <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = ${san_list}
EOF
  openssl x509 -req -days "$CERT_DAYS" -in "$csr" \
    -CA "${CA_DIR}/ca.crt" -CAkey "${CA_DIR}/ca.key" -CAcreateserial \
    -out "$crt" -extfile "$ext" 2>/dev/null
  rm -f "$csr" "$ext"
  echo "Issued ${crt} (SANs: ${san_list})" >&2
fi

# Always refresh the CA copy so a re-created CA never leaves a stale trust file behind.
cp "${CA_DIR}/ca.crt" "${out}/ca.crt"
chmod 644 "$crt" "$key" "${out}/ca.crt"
