#!/usr/bin/env bash
# check-lan-hostnames.sh — refuse a real internal LAN identifier in tracked files (#874).
#
# Two independent checks, because a guard built to only ONE of these patterns has already
# let a real leak through this repo's history (issue #874's own three prior scrub attempts,
# each of which caught the `user@<RFC1918-IP>` shape and missed a bare hostname form):
#
#   (a) the literal internal identifier — a specific username, hardcoded below because
#       this repo has actually leaked it. Zero false positives by construction: it is one
#       exact string. Catches ANY occurrence regardless of surrounding syntax
#       (`user@host`, a bare `host:~/path`, prose) — an absence-shaped leak (a bare
#       hostname with no `@`) is exactly what rule (b) alone would miss.
#   (b) the GENERIC `user@<RFC1918-IP>` shape, so the NEXT username/IP this repo's docs
#       accidentally leak is still caught. This is the pattern issue #874's own proposed
#       fix named, and it is deliberately kept alongside (a) rather than instead of it.
#
# Usage: check-lan-hostnames.sh <file> [<file> ...]
# Exit 0 if clean, 1 and a list of offending lines otherwise.
#
# Placeholder documentation forms (user@mac-studio.local, user@192.168.1.100,
# username@...) are allowlisted for rule (b) by construction: rule (b) only fires on an
# RFC1918 host that is NOT one of the documented placeholder IPs, and rule (a) only fires
# on the actual leaked identifier, never on the word "user"/"username" alone.
#
# Single grep invocation per rule across ALL given files (not one process per file) —
# this runs on every commit, and a per-file subprocess loop measurably slows both the
# hook and the whole-tracked-tree regression test that exercises it.

set -uo pipefail

# ─── Rule (a): the literal leaked identifier ────────────────────────────────────────
# Deliberately not sourced from anywhere else in the repo: the whole point is that this
# string must never appear in a tracked file again, including inside a variable meant to
# hold it.
LEAKED_IDENTIFIERS=(
  "superstudio"
)

# ─── Rule (b): generic user@<RFC1918-IP>, allowlisting the documented placeholders ──
# 192.168.1.100 is scripts/setup-remote-builder.sh's and scripts/README.md's own
# documented placeholder IP — never flag it.
ALLOWED_PLACEHOLDER_IPS_RE='192\.168\.1\.100'

RFC1918_USER_AT_IP_RE='[A-Za-z0-9._-]+@((10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})|(172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})|(192\.168\.[0-9]{1,3}\.[0-9]{1,3}))'

# This script's own path must never trigger on itself — the identifiers/IPs above are
# data, and a naive scan of this file would report a false positive on its own source.
# A suffix match (not a resolved-path comparison) deliberately avoids a
# fork+cd+pwd per candidate file: this runs over the whole tracked tree on every
# commit, and that resolution alone measured as the dominant cost here.
SELF_NAME="$(basename "${BASH_SOURCE[0]}")"

files=()
for file in "$@"; do
  [ -f "$file" ] || continue
  case "$file" in
    */"$SELF_NAME" | "$SELF_NAME") continue ;;
  esac
  files+=("$file")
done

exit_code=0

if [ "${#files[@]}" -gt 0 ]; then
  # Rule (a): one case-insensitive grep across every identifier and every file.
  identifier_pattern=$(IFS='|'; echo "${LEAKED_IDENTIFIERS[*]}")
  if hits=$(grep -inHE "$identifier_pattern" "${files[@]}" 2>/dev/null); then
    echo "$hits" | while IFS= read -r line; do
      echo "$line: leaked identifier" >&2
    done
    exit_code=1
  fi

  # Rule (b): one grep across all files, then filter out the documented placeholder IP.
  if hits=$(grep -nHE "$RFC1918_USER_AT_IP_RE" "${files[@]}" 2>/dev/null); then
    real_hits=$(echo "$hits" | grep -vE "$ALLOWED_PLACEHOLDER_IPS_RE" || true)
    if [ -n "$real_hits" ]; then
      echo "$real_hits" | while IFS= read -r line; do
        echo "$line: user@<RFC1918-IP> pattern" >&2
      done
      exit_code=1
    fi
  fi
fi

if [ "$exit_code" -ne 0 ]; then
  echo "" >&2
  echo "❌ Found real/leaked internal LAN identifier(s) above." >&2
  echo "If this is a genuine documentation placeholder, use the repo's existing" >&2
  echo "convention: user@mac-studio.local or user@192.168.1.100 (never a real" >&2
  echo "internal hostname, username, or IP)." >&2
fi

exit "$exit_code"
