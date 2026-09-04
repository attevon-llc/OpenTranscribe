#!/bin/bash
# model-cache.sh — seeding the release-test model cache.
#
# ⚠️ THE INVARIANT THIS FILE EXISTS FOR
#
# No file under a model cache's ``nltk_data`` may be multiply linked
# (``st_nlink > 1``).
#
# nltk 3.10 added ``pathsec`` hardening against CWE-59 (link following). It
# REFUSES to open any file under ``nltk_data`` whose link count is above one,
# on the reasoning that a hardlink can point at an inode outside the data
# root. ``requirements.txt`` pins ``nltk==3.10.3`` deliberately — the pin
# comment reads "pathsec hardening is required, not avoided" — so this is a
# property the application depends on, not an accident to work around.
#
# Both rehearsal scenarios used to seed their per-run cache with
# ``rsync -a --link-dest=<src> <src> <dst>``. Passing the SAME directory as
# both source and link-dest makes rsync hardlink every file rather than copy
# it — which is exactly the point (a ~5 GB cache costs no disk) and is
# perfectly safe for the HuggingFace, torch, sentence-transformers and
# pyannote trees. Applied to ``nltk_data`` it raises every file to
# ``st_nlink >= 2`` on the first run, and nltk then refuses to tokenize.
#
# The observed symptom was NOT an nltk error. It was every transcription in
# both scenarios ending in ``status=error``, with the real cause only in the
# ``media_file.last_error_message`` column of a database the harness tore down
# on its way out:
#
#     Security Violation [pathsec.open]: refusing multiply-linked file
#     '…/nltk_data/tokenizers/punkt_tab/english/collocations.tab'
#     (st_nlink=3); a hardlink can point at an outside-root inode (CWE-59)
#
# So: hardlink the big trees, COPY the pathsec-sensitive one, and assert the
# invariant at seed time so a regression fails here — loudly, in seconds —
# instead of ten minutes later as an opaque pipeline failure.
#
# Depends on guardrails.sh for gr_log/gr_ok/gr_warn/gr_die; source it first.

# Cache subdirectories whose consumer refuses multiply-linked files. Keep this
# a list rather than a single value: it is a property of the CONSUMER library,
# and a second hardened library would join nltk here rather than fork the code.
MC_PATHSEC_SUBDIRS=("nltk_data")

mc_is_pathsec_subdir() {
    local candidate="$1" sub
    for sub in "${MC_PATHSEC_SUBDIRS[@]}"; do
        [[ "$candidate" == "$sub" ]] && return 0
    done
    return 1
}

# Cache subdirectories that must be a REAL COPY for a DIFFERENT reason than
# MC_PATHSEC_SUBDIRS above: not because the consumer refuses a hardlink, but
# because the consumer is ALLOWED to rewrite the files in place, and a
# hardlink would let that rewrite reach back through the shared cache to a
# live source it must never touch.
#
# diar-native (issue #670): backend/app/transcription/native_provision.py
# runs `diar-server provision-models` from the FastAPI lifespan on every
# backend boot. It is idempotent — it short-circuits on a VALID
# diar-provision.json marker and only re-exports when the marker is judged
# stale — but "valid" is judged by whichever diar-server BINARY is running,
# and test-upgrade.sh deliberately runs an OLDER release's binary against
# these exact files (its FROM stage, phase 03/04). If that older binary's
# own exporter_version/model_set expectations disagree with the marker on
# disk, it re-exports, OVERWRITING the named .onnx/.npy files IN PLACE.
# Unlike HuggingFace's content-addressed blob store (a changed model gets a
# NEW blob under a new hash, so an old hardlinked blob is never touched),
# diar-native's files are the same path every time — a rewrite is a rewrite
# of whatever the path is linked to.
#
# This is not hypothetical for THIS harness specifically:
# test-upgrade.sh's shared cache (lib.model-cache.sh callers, phase 03) is
# mounted directly as MODEL_CACHE_DIR for the stacks under test — there is
# no per-run copy, unlike test-fresh-install.sh — and that shared cache is
# itself seeded by HARDLINK from this host's own live, currently-in-use
# model cache. A hardlinked diar-native subtree means a re-export during a
# rehearsal corrupts the live host's diarizer weights.
#
# Checked before assuming this belongs here, the same way nltk's inclusion
# was checked: unlike nltk, nothing in diar-server / native_provision.py /
# the `ort` crate refuses to open a multiply-linked file (no st_nlink /
# CWE-59-style guard found anywhere in the export or serving path) — so this
# is NOT a pathsec-shaped crash, it is a silent-corruption risk, and a real
# copy is the same fix for a different reason. Deliberately NOT merged into
# MC_PATHSEC_SUBDIRS: that list's own name, and every message
# mc_assert_no_hardlinks/mc_break_hardlinks print, specifically claim "nltk
# pathsec refuses" — folding a corruption concern in there would make those
# messages lie about a directory that never refuses anything.
MC_NO_HARDLINK_SUBDIRS=("diar-native")

mc_is_no_hardlink_subdir() {
    local candidate="$1" sub
    for sub in "${MC_NO_HARDLINK_SUBDIRS[@]}"; do
        [[ "$candidate" == "$sub" ]] && return 0
    done
    return 1
}

# Break every hardlink under a directory by replacing each multiply-linked
# file with an independent copy of itself. Idempotent, and a no-op when the
# tree is already clean.
mc_break_hardlinks() {
    local dir="$1"
    [[ -d "$dir" ]] || return 0

    local broken=0 failed=0 f tmp
    while IFS= read -r -d '' f; do
        tmp="${f}.mc-unlink.$$"
        if cp -p "$f" "$tmp" 2>/dev/null && mv -f "$tmp" "$f" 2>/dev/null; then
            broken=$(( broken + 1 ))
        else
            rm -f "$tmp" 2>/dev/null || true
            failed=$(( failed + 1 ))
        fi
    done < <(find "$dir" -type f -links +1 -print0 2>/dev/null)

    if (( failed > 0 )); then
        gr_warn "could not break $failed hardlink(s) under $dir"
    fi
    if (( broken > 0 )); then
        gr_ok "broke $broken hardlink(s) under $dir (this cache subdir must not be multiply-linked)"
    fi
    return 0
}

# Hard-fail if any file under a directory is still multiply linked. This is
# the gate: it converts a silent, ten-minutes-later transcription failure into
# an immediate one that names the cause.
mc_assert_no_hardlinks() {
    local dir="$1"
    local context="${2:-model cache}"
    [[ -d "$dir" ]] || return 0

    local offenders
    offenders=$(find "$dir" -type f -links +1 2>/dev/null | head -5)
    if [[ -n "$offenders" ]]; then
        local count
        count=$(find "$dir" -type f -links +1 2>/dev/null | wc -l)
        gr_die "$context: $count file(s) under $dir are multiply linked (st_nlink>1)." \
               $'\n'"       nltk >=3.10 pathsec refuses these (CWE-59) and EVERY transcription" \
               $'\n'"       will fail with 'Security Violation [pathsec.open]'." \
               $'\n'"       First offenders:"$'\n'"$offenders"
    fi
    gr_ok "$context: no multiply-linked files under $(basename "$dir") (nltk pathsec safe)"
}

# Seed one cache subdirectory from a source tree.
#
# Hardlinks (cheap, no disk cost) for ordinary trees; a real copy for the
# pathsec-sensitive ones (MC_PATHSEC_SUBDIRS) and the ones whose consumer can
# rewrite files in place (MC_NO_HARDLINK_SUBDIRS). Falls back to a plain copy
# if rsync is unavailable or fails, because a slow correct seed beats a fast
# broken one.
mc_seed_subdir() {
    local src_root="$1" dst_root="$2" sub="$3"
    local src="$src_root/$sub" dst="$dst_root/$sub"

    [[ -d "$src" ]] || return 0
    mkdir -p "$dst"

    if mc_is_pathsec_subdir "$sub" || mc_is_no_hardlink_subdir "$sub"; then
        # Real copy — see the header (nltk pathsec refusal, or a consumer
        # that can rewrite in place, e.g. diar-native). --no-links so a
        # symlink in the source cannot smuggle a link into the copy either.
        if command -v rsync >/dev/null 2>&1; then
            rsync -a --copy-links "$src/" "$dst/" 2>/dev/null || \
                cp -rL "$src/." "$dst/" 2>/dev/null || \
                gr_warn "could not seed $sub — it will download on first start"
        else
            cp -rL "$src/." "$dst/" 2>/dev/null || \
                gr_warn "could not seed $sub — it will download on first start"
        fi
        # The SOURCE tree may itself already be poisoned by an older run that
        # hardlinked it; a faithful copy of a linked tree can inherit the
        # links, so normalise the destination unconditionally.
        mc_break_hardlinks "$dst"
    else
        if command -v rsync >/dev/null 2>&1; then
            rsync -a --link-dest="$src/" "$src/" "$dst/" 2>/dev/null || \
                cp -rL "$src/." "$dst/" 2>/dev/null || \
                gr_warn "could not seed $sub — it will download on first start"
        else
            cp -rL "$src/." "$dst/" 2>/dev/null || \
                gr_warn "could not seed $sub — it will download on first start"
        fi
    fi
    return 0
}

# Seed a whole cache: every subdirectory present in the source, then assert
# the pathsec invariant on the result.
mc_seed_cache() {
    local src_root="$1" dst_root="$2"
    shift 2
    local subs=("$@")

    if [[ ${#subs[@]} -eq 0 ]]; then
        subs=(huggingface torch nltk_data sentence-transformers pyannote diar-native)
    fi

    local sub
    for sub in "${subs[@]}"; do
        mc_seed_subdir "$src_root" "$dst_root" "$sub"
    done

    for sub in "${MC_PATHSEC_SUBDIRS[@]}"; do
        mc_assert_no_hardlinks "$dst_root/$sub" "seeded model cache"
    done
    return 0
}
