#!/usr/bin/env bash
# Sourced by scripts/e2e/update-visual-baselines.sh: the library every baseline is photographed
# against. Needs the caller's log(), die(), colours, REPO_ROOT, REPORT_PY, WORKDIR, DRY_RUN and
# EXIT_PRECONDITION, and (for seed_capture_corpus) stack-target.sh's st_container on a resolved
# capture stack.
#
# The corpus is a fixed slice of the public demo's seed bundle (scripts/e2e/visual-seed-corpus.json),
# never the benchmark recordings: every committed image is public, and the benchmark corpus is
# podcast clips with political talk whose filenames leaked into the images (#43). The slice is
# imported with its PRECOMPUTED transcripts and conversations (`demo_seed import`), so the seed
# runs no speech recognition and needs no GPU. The bundle lives outside the repository, like the
# demo's: name it with DEMO_SEED_BUNDLE (the variable scripts/demo/demo-reset.sh reads).
#
# REPORT_PY is enough here: `demo_seed subset` is stdlib-only, so the manifest can be derived in
# CI and in a checkout without backend/venv.

VISUAL_SEED_CORPUS="${VISUAL_SEED_CORPUS:-$REPO_ROOT/scripts/e2e/visual-seed-corpus.json}"
#: The account the capture logs in as (test_visual_regression.py reads the same E2E_ADMIN_*
#: variables, defaulting to the dev stack's seeded admin); the slice is imported into it.
VISUAL_SEED_EMAIL="${E2E_ADMIN_EMAIL:-admin@example.com}"
VISUAL_SEED_PASSWORD="${E2E_ADMIN_PASSWORD:-password}"
SEED_BUNDLE_DIR=""
CORPUS_ID=""
CORPUS_VERSION=""
CORPUS_BUNDLE_VERSION=""
CORPUS_ITEMS=""

# `demo_seed subset` run from backend/, where the `app` package resolves; extra args pass through.
_demo_seed_subset() {
    (cd "$REPO_ROOT/backend" && "$REPORT_PY" -m app.scripts.demo_seed subset \
        --bundle "$SEED_BUNDLE_DIR" --items "$CORPUS_ITEMS" \
        --bundle-version "$CORPUS_BUNDLE_VERSION" "$@")
}

# The files the import WILL create, as `<media file><TAB><bytes>` lines under `# source:`,
# `# corpus:` and `# bundle_version:` headers (corpus_json and assert_corpus_is_the_seed read
# them). Derived by validating the slice in the bundle itself, never from a copy of the names,
# and refused HERE, before a multi-minute stack start, when the bundle is missing, is another
# version than the corpus pins, or does not hold every item with an allowed licence.
derive_seed_manifest() {
    local bundle="${DEMO_SEED_BUNDLE:-}" fields lines line count=0
    [[ -n "$REPORT_PY" ]] || die "no python interpreter for the seed corpus (neither \
backend/venv nor python3 on PATH)" "$EXIT_PRECONDITION"
    if [[ -z "$bundle" || ! -f "$bundle/manifest.json" ]]; then
        die "DEMO_SEED_BUNDLE must name the demo seed bundle (a directory with manifest.json); \
baselines are photographed against its neutral slice in ${VISUAL_SEED_CORPUS#"$REPO_ROOT"/}, \
never the benchmark corpus. Example: DEMO_SEED_BUNDLE=/path/to/demo-bundle $0 --reason ..." \
            "$EXIT_PRECONDITION"
    fi
    SEED_BUNDLE_DIR="$(cd "$bundle" && pwd)"
    fields="$("$REPORT_PY" -I -c '
import json, sys
c = json.load(open(sys.argv[1], encoding="utf-8"))
print(c["corpus_id"], c["corpus_version"], c["bundle_version"], ",".join(c["items"]))
' "$VISUAL_SEED_CORPUS")" || die "unreadable corpus file ${VISUAL_SEED_CORPUS}" "$EXIT_PRECONDITION"
    read -r CORPUS_ID CORPUS_VERSION CORPUS_BUNDLE_VERSION CORPUS_ITEMS <<< "$fields"
    lines="$(_demo_seed_subset)" || die "the seed bundle ${SEED_BUNDLE_DIR} does not hold \
corpus ${CORPUS_ID} v${CORPUS_VERSION} (bundle ${CORPUS_BUNDLE_VERSION}); see above" \
        "$EXIT_PRECONDITION"
    SEED_MANIFEST="# source: ${CORPUS_ID}
# corpus: ${CORPUS_ID} ${CORPUS_VERSION}
# bundle_version: ${CORPUS_BUNDLE_VERSION}
${lines}"
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == "#"* ]] && continue
        count=$((count + 1))
    done <<< "$SEED_MANIFEST"
    [[ $count -gt 0 ]] || die "corpus ${CORPUS_ID} selects no file" "$EXIT_PRECONDITION"
    log "${BLUE}🌱 Expected seed corpus: ${count} file(s), ${CORPUS_ID} v${CORPUS_VERSION} \
from demo bundle ${CORPUS_BUNDLE_VERSION}${NC}"
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == "#"* ]] && continue
        log "     ${line//$'\t'/  }  bytes"
    done <<< "$SEED_MANIFEST"
}

# Import the slice into the capture stack's admin account. `--if-empty` makes a reused stack
# safe: an account that already owns files is left alone, and assert_corpus_is_the_seed then
# decides whether what it owns is exactly the corpus. The slice is written to WORKDIR and copied
# into the label-scoped backend container (scripts/release-tests/isolated-prod-env.sh does the
# same for the demo bundle); DEMO_MODE=false is set for that one process only.
seed_capture_corpus() {
    $DRY_RUN && return 0
    local slice="$WORKDIR/seed-bundle" ctr dest
    log "${BLUE}🌱 Importing ${CORPUS_ID} v${CORPUS_VERSION} into ${VISUAL_SEED_EMAIL} (no transcription)…${NC}"
    _demo_seed_subset --out "$slice" >/dev/null || { log "${RED}❌ could not write the corpus slice${NC}"; return 1; }
    ctr="$(st_container backend)" || ctr=""
    [[ -n "$ctr" ]] || { log "${RED}❌ no running backend container in ${ST_PROJECT:-?}${NC}"; return 1; }
    dest="/tmp/ot-visual-seed-$(basename "$WORKDIR")"
    docker cp "$slice" "$ctr:$dest" || return 1
    docker exec -e DEMO_MODE=false -e DEMO_USER_EMAIL="$VISUAL_SEED_EMAIL" \
        -e DEMO_USER_PASSWORD="$VISUAL_SEED_PASSWORD" "$ctr" \
        python -m app.scripts.demo_seed import --bundle "$dest" --if-empty
}
