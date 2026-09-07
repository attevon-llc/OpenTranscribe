#!/bin/bash
# The release-asset set `finish` must attach — SBOMs per published leg, each checksummed —
# derived, never transcribed. Sourced by scripts/release/95-finish.sh, same pattern as
# scripts/release/published-repos.sh (sourced by both 90-promote.sh and 95-finish.sh).
#
# WHY THIS EXISTS (issue #781)
#
# 95-finish.sh used to attach release assets with a bare existence glob:
#
#   for f in dist/opentranscribe-offline-*.tar.gz dist/opentranscribe-windows-*.zip \
#            security-reports/*-sbom.json; do
#       [[ -f "$f" ]] && assets+=("$f")
#   done
#
# Two defects, and they compound rather than cancel:
#
#   * ABSENCE was silent. If security-reports/*-sbom.json produced nothing — a failed scan,
#     a skipped stage, a relocated OT_SCAN_DIR — the loop simply appended nothing and
#     `gh release create` succeeded anyway. A release could ship with zero SBOMs and pass.
#   * PRESENCE was not evidence either. security-reports/ is git-tracked and, before this
#     change, held three stale pre-#667 SBOMs (backend-sbom.json, frontend-sbom.json,
#     docs-sbom.json — no arch qualifier, one per component, committed in fb3094ee). The glob
#     would have attached all three to every future release, describing whatever version they
#     were last regenerated against, not the one being published.
#
# Same failure shape this repo already fixed twice: a gate that reports success when it could
# not do its job (#413 fail-open scan, #681 unscannable component reporting a pass). The fix
# here is the same discipline #667/#681 established for the scan stage: derive the expected
# set from the single source of truth, verify presence AND content, and never treat "could not
# check" as "nothing to check".
#
# `dist/opentranscribe-offline-*.tar.gz` / `dist/opentranscribe-windows-*.zip` are gone
# entirely, not fixed: `dist/` was referenced by that one line and created by nothing (the
# offline package actually lands at offline-package-build/linux/*.tar.**xz**, and the Windows
# builder produces no archive at all — it stages a directory for Inno Setup on Windows). The
# owner's standing decision is that both packages are developer-built and NOT distributed via
# the GitHub Release — see releasing.md and the two `warn`-severity, `waived` criteria in
# release-criteria.yaml. This file therefore only ever derives SBOM + checksum assets.

# Emit `<component>-<arch>-sbom.json` for every (component, platform) leg that BOTH:
#   * is declared by `docker-build-push.sh list-platforms` (the same source 40-build.sh and
#     50-scan.sh use for what gets built/scanned), and
#   * is in the caller-supplied repos_tsv — the already-validated, ACTUALLY PUBLISHED
#     component set from published-repos.sh (never a second, independently-hardcoded list).
#
# `blackwell` is excluded even if the platform table lists it: it publishes a `:blackwell`
# tag, never a `:vX.Y.Z` one (see published-repos.sh's own comment), so it is never in
# repos_tsv and this loop's membership check already drops it — the explicit `continue` below
# is belt-and-braces documentation, not load-bearing.
#
# Naming matches security-scan.sh's `scan_label()`: the arch suffix is unconditional, even for
# a single-platform component, so a missing arch can never look like an ordinary report.
#
# Args:   $1 = repos_tsv ("component<TAB>repo" per line, from release_published_repos_or_die)
# Stdout: one filename per line.
# Return: 0 if at least one leg was derived, 1 otherwise — "derived zero legs" is COULD NOT
#         COMPUTE, never "nothing to publish", and callers must treat it that way (not-measured
#         + fail_out), matching the rule scan.sh's own legs_expected==0 branch applies.
release_assets_expected_sboms() {
    local repos_tsv="$1"
    local repo_root="${REPO_ROOT:-.}"

    [[ -n "$repos_tsv" ]] || return 1

    local -A published=()
    local component _repo
    while IFS=$'\t' read -r component _repo; do
        [[ -n "$component" ]] && published["$component"]=1
    done <<< "$repos_tsv"
    (( ${#published[@]} > 0 )) || return 1

    local platform_tsv
    platform_tsv="$("${repo_root}/scripts/docker-build-push.sh" list-platforms 2>/dev/null)" || true
    [[ -n "$platform_tsv" ]] || return 1

    local _capability platforms platform
    local -a plats
    local emitted=0
    while IFS=$'\t' read -r component _capability platforms; do
        [[ -n "$component" ]] || continue
        [[ "$component" == "blackwell" ]] && continue
        [[ -n "${published[$component]:-}" ]] || continue
        IFS=',' read -r -a plats <<< "$platforms"
        for platform in "${plats[@]}"; do
            [[ -n "$platform" ]] || continue
            printf '%s-%s-sbom.json\n' "$component" "${platform#linux/}"
            emitted=$((emitted + 1))
        done
    done <<< "$platform_tsv"

    (( emitted > 0 ))
}

# Read metadata.component.name + metadata.component.version back out of a CycloneDX SBOM
# and confirm it names THIS release. Syft (security-scan.sh's generate_sbom) writes both
# fields; this is the same read-it-back-out-of-the-artifact assertion 50-scan.sh makes of
# Trivy's ArtifactName field, applied to the SBOM instead.
#
# stdlib `json` only, deliberately: criteria-lib.sh's python fallback is bare `python3`,
# which is not guaranteed to have PyYAML, but the standard library always has `json`.
#
# An unreadable/corrupt/missing file, or an empty component name, is treated exactly like a
# version mismatch (return 1) — "could not read what version this describes" is never a pass.
#
# Args:   $1 = path to the SBOM file, $2 = the expected version string (e.g. "v0.5.0")
# Return: 0 = names this version. 1 = does not (includes unreadable/corrupt/missing).
release_assets_sbom_matches_version() {
    local sbom_file="$1" version="$2"
    [[ -f "$sbom_file" ]] || return 1
    python3 - "$sbom_file" "$version" <<'PY' 2>/dev/null
import json
import sys

path, version = sys.argv[1], sys.argv[2]
try:
    with open(path) as handle:
        doc = json.load(handle)
    component = (doc.get("metadata") or {}).get("component") or {}
    name = component.get("name")
    found_version = component.get("version")
except Exception:
    sys.exit(1)
sys.exit(0 if name and found_version == version else 1)
PY
}

# Checksum every given asset into a fresh mktemp -d scratch dir, cleaned up on the calling
# script's EXIT — NEVER into git-tracked security-reports/, which would turn a release
# artifact into a repo commit. One `<basename>.sha256` sidecar per asset, plus one combined
# SHA256SUMS covering all of them (the format `sha256sum -c` verifies directly).
#
# Reuses, never recomputes, a checksum a builder already wrote (build-offline-package.sh's
# `sha256sum "$file" > "$file.sha256"` convention at :531) — a second implementation of "what
# is this file's digest" is a second chance for the two to disagree.
#
# ⚠️ NOT `$(release_assets_checksum_dir ...)`. A `trap ... EXIT` set inside a command
# substitution fires when THAT SUBSHELL exits — i.e. immediately, deleting the directory
# before the caller can read anything from it (confirmed while writing this: the scratch dir
# was gone by the time the assignment completed). So this sets a variable instead of printing
# to stdout, and must be called as a plain statement, in the CALLER's own shell, so the trap
# it installs governs the caller's real exit:
#
#   release_assets_checksum_dir "${assets[@]}"
#   checksum_dir="$RELEASE_ASSETS_CHECKSUM_DIR"
#
# Args: the asset file paths (assumed to already exist).
# Sets: RELEASE_ASSETS_CHECKSUM_DIR to the scratch directory path.
release_assets_checksum_dir() {
    local scratch
    scratch="$(mktemp -d "${TMPDIR:-/tmp}/release-assets-checksums.XXXXXX")" || return 1
    # shellcheck disable=SC2064  # intentional: expand $scratch NOW, not at trap-fire time.
    trap "rm -rf '${scratch}'" EXIT

    local sums_file="${scratch}/SHA256SUMS"
    : > "$sums_file"

    local asset base digest
    for asset in "$@"; do
        [[ -f "$asset" ]] || continue
        base="$(basename "$asset")"
        if [[ -f "${asset}.sha256" ]]; then
            # A builder already produced one for this exact file — reuse it verbatim rather
            # than re-hashing, so there is only ever one opinion about this file's digest.
            cp "${asset}.sha256" "${scratch}/${base}.sha256"
            digest="$(awk '{print $1}' "${asset}.sha256")"
        else
            digest="$(sha256sum "$asset" | awk '{print $1}')"
            printf '%s  %s\n' "$digest" "$base" > "${scratch}/${base}.sha256"
        fi
        printf '%s  %s\n' "$digest" "$base" >> "$sums_file"
    done

    # shellcheck disable=SC2034  # read by the caller (95-finish.sh), a different file —
    # this is the documented "output variable" contract this function's header describes.
    RELEASE_ASSETS_CHECKSUM_DIR="$scratch"
}

# Resolve where THIS version's scan reports live.
#
# 50-scan.sh writes .release/<version>/scan-dir unconditionally, as soon as it resolves its
# own OUT_DIR — so a scan that ran against a relocated OT_SCAN_DIR leaves a breadcrumb this
# stage can follow. Without this, a relocated scan followed by a bare
# `./scripts/release.sh finish` would silently look in ./security-reports, find nothing, and
# (before this file existed) ship no SBOM at all.
#
# Preference order: the marker 50-scan.sh actually wrote for THIS version, then the
# OT_SCAN_DIR env var (an operator's own override, e.g. re-running finish in a fresh shell),
# then the same ./security-reports default 50-scan.sh itself falls back to.
#
# Args: $1 = version (e.g. "v0.5.0")
release_assets_resolve_scan_dir() {
    local version="$1"
    local marker=".release/${version}/scan-dir"
    if [[ -s "$marker" ]]; then
        head -n1 "$marker"
        return 0
    fi
    printf '%s\n' "${OT_SCAN_DIR:-./security-reports}"
}
