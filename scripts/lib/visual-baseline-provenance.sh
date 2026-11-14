#!/usr/bin/env bash
# Sourced by scripts/e2e/update-visual-baselines.sh: git provenance, the per-baseline JSON sidecar
# writer and its JSON helpers. Needs the caller's log(), colours, REPO_ROOT, SELECTED_SURFACES,
# SCREENSHOT_DIR, PROVENANCE_DIR, WORKDIR, REASON, FRESH_NAME, PORT_OFFSET, GPU_DEVICE, SEED_MANIFEST.

# ---------------------------------------------------------------------------
# Provenance: WHICH CODE produced this image, not only why it changed.
# ---------------------------------------------------------------------------
GIT_SHA=""
GIT_BRANCH=""
GIT_DIRTY="false"
GIT_DIRTY_COUNT=0

resolve_git_provenance() {
    local porcelain
    GIT_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
    GIT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    porcelain="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null || true)"
    if [[ -n "$porcelain" ]]; then
        GIT_DIRTY="true"
        GIT_DIRTY_COUNT="$(printf '%s\n' "$porcelain" | grep -c '' || true)"
    fi
}

# A dirty tree is WARNED about and recorded, not refused.
#
# Refusing would be wrong: the normal shape of this work is "change the UI, look
# at it, recapture" and demanding a commit first only encourages a throwaway one.
# But a baseline captured from uncommitted source is not reproducible by anyone
# else — the sha in its sidecar does not describe the pixels — so it must never
# be silently indistinguishable from a clean capture. Hence: loud here, and
# `git_dirty: true` in every sidecar the run writes.
warn_if_dirty() {
    $GIT_DIRTY || return 0
    log ""
    log "${YELLOW}⚠ The working tree is DIRTY (${GIT_DIRTY_COUNT} changed path(s)).${NC}"
    log "${YELLOW}  Baselines captured now cannot be reproduced from ${GIT_SHA:0:12} by anyone"
    log "  else, because the pixels come partly from uncommitted work. This will be"
    log "  recorded as \"git_dirty\": true in each sidecar. Commit first if you want a"
    log "  baseline someone else can regenerate.${NC}"
    log ""
}

# One JSON sidecar per captured baseline. See the header for why per-surface and
# why not inside __screenshots__/.
record_provenance() {
    local surface theme name status pct captured_at row_name row_status row_pct
    captured_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    mkdir -p "$PROVENANCE_DIR"

    for surface in "${SELECTED_SURFACES[@]}"; do
        for theme in light dark; do
            name="${surface}-${theme}"
            [[ -f "$SCREENSHOT_DIR/${name}.png" ]] || continue
            status=""
            pct=""
            if [[ -f "$WORKDIR/report.tsv" ]]; then
                while IFS=$'\t' read -r row_name row_status row_pct; do
                    if [[ "$row_name" == "$name" ]]; then
                        status="$row_status"
                        pct="$row_pct"
                    fi
                done < "$WORKDIR/report.tsv"
            fi
            cat > "$PROVENANCE_DIR/${name}.json" <<EOF
{
  "baseline": "__screenshots__/${name}.png",
  "captured_at": "${captured_at}",
  "git_sha": "${GIT_SHA}",
  "git_branch": "${GIT_BRANCH}",
  "git_dirty": ${GIT_DIRTY},
  "git_dirty_paths": ${GIT_DIRTY_COUNT},
  "reason": $(json_string "$REASON"),
  "captured_by": "scripts/e2e/update-visual-baselines.sh",
  "stack": {
    "compose_project": "otfresh-${FRESH_NAME}",
    "port_offset": ${PORT_OFFSET},
    "gpu_device": $([[ -n "$GPU_DEVICE" ]] && echo "$GPU_DEVICE" || echo 'null'),
    "seeded": true,
    "mode": "dev (local source bind-mounted)"
  },
  "corpus": $(corpus_json),
  "change_from_previous": {
    "status": $(json_string "${status:-unknown}"),
    "differing_pixels": $(json_string "${pct:-}")
  }
}
EOF
        done
    done
    log "${GREEN}✔ Provenance written to ${PROVENANCE_DIR#"$REPO_ROOT"/}/${NC}"
    $GIT_DIRTY && log "${YELLOW}  …recorded as captured from a DIRTY tree.${NC}"
    return 0
}

# The corpus this image was photographed against, from the manifest the library was asserted
# to equal. "3 smallest files in a gitignored host directory" is host state, so without this a
# new small file in that directory silently changes every future baseline and nothing says so.
# `seed_source` is the directory's last component only: a committed sidecar must not carry the
# photographer's home path.
corpus_json() {
    local line source="" name size entries="" count=0
    while IFS= read -r line; do
        case "$line" in
            "# source:"*) source="${line#"# source:"}"; source="${source# }"; source="${source##*/}" ;;
            "#"*|"") ;;
            *)
                name="${line%%$'\t'*}"
                size="${line#*$'\t'}"
                [[ $count -gt 0 ]] && entries+=", "
                entries+="{\"filename\": $(json_string "$name"), \"size\": ${size}}"
                count=$((count + 1))
                ;;
        esac
    done <<< "$SEED_MANIFEST"
    printf '{"seed_source": %s, "files": [%s], "library_total": %d}' \
        "$(json_string "$source")" "$entries" "$count"
}

# Minimal JSON string escaping for operator-supplied text (--reason).
json_string() {
    local s="${1//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/ }"
    s="${s//$'\t'/ }"
    printf '"%s"' "$s"
}
