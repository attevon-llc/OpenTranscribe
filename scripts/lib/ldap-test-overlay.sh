#!/bin/bash
# Compose-chain helper for `./opentr.sh ... --with-ldap-test`. Sourced by opentr.sh (it reads and
# appends to its COMPOSE_FILES / WITH_LDAP_TEST_FLAG), never run on its own.

# --with-ldap-test: LLDAP serves LDAPS and the backend refuses plaintext binds and verifies the
# certificate, so the overlay needs a server certificate the backend can verify. Issue it from the
# test CA BEFORE compose starts (a missing bind-mount source would be created as an empty
# directory) and mount the CA into the services that open LDAP connections. Call right after
# add_flag_overlay WITH_LDAP_TEST_FLAG.
add_ldap_test_trust_overlay() {
  [ -n "${WITH_LDAP_TEST_FLAG:-}" ] || return 0
  [ -f docker-compose.ldap-test.yml ] || return 0 # add_flag_overlay already warned
  local trust_file="docker-compose.ldap-test-trust.yml"
  if [ ! -f "$trust_file" ]; then
    echo "⚠️  --with-ldap-test specified but $trust_file not found"
    return 0
  fi
  local cert_log
  if ! cert_log="$(./scripts/pki/issue-test-server-cert.sh ldap-test \
    DNS:lldap-test DNS:lldap DNS:localhost IP:127.0.0.1 2>&1)"; then
    echo "$cert_log" >&2
    echo "❌ Failed to issue the LDAP test server certificate (scripts/pki/issue-test-server-cert.sh)"
    exit 1
  fi
  COMPOSE_FILES="$COMPOSE_FILES -f $trust_file"
}
