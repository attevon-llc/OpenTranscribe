#!/bin/bash
# Postgres snapshot/restore primitives for the rollback tail of test-upgrade.sh
# (phases 06b, 13-17; issue #598).
#
# Deliberately separate from assertions.sh (pure comparison, no Docker
# dependency — test-fresh-install.sh and test-lite-mode.sh source it too) and
# from api-client.sh (HTTP only). Everything here shells out to `docker exec
# <container> psql ...`, matching the pattern test-upgrade.sh's own
# snapshot_state() already uses for the container name, rather than
# `docker compose exec`, which would need a compose file + project context
# this library does not carry.
#
# Every function takes the postgres CONTAINER name, DB user, and DB name
# explicitly rather than reading globals, so it is usable standalone against a
# throwaway container (see selftest-rollback-fault-injection.sh) as well as
# against the scenario's real 'opentranscribe-postgres'.

set -euo pipefail

dbs_log()  { echo -e "\033[0;34m[db-snapshot]\033[0m $*" >&2; }
dbs_warn() { echo -e "\033[1;33m[db-snapshot]\033[0m $*" >&2; }

# dbs_dump CONTAINER USER DB OUT_PATH
#   Plain `pg_dump` into OUT_PATH, chmod 600 (dumps hold every seeded user's
#   transcripts in plaintext). Non-zero on any failure, and never leaves a
#   partial file behind.
dbs_dump() {
    local container="$1" user="$2" db="$3" out="$4"
    if ! docker exec "$container" pg_dump -U "$user" "$db" > "$out"; then
        rm -f "$out"
        dbs_warn "pg_dump of $db failed"
        return 1
    fi
    chmod 600 "$out"
}

# dbs_verify_dump_restores CONTAINER USER DUMP SCRATCH_DB
#   Restores DUMP into a throwaway SCRATCH_DB in the SAME container/cluster —
#   never the database under test — and echoes the resulting media_file row
#   count. This is what turns "the backup is good" into a measurement instead
#   of `[ -s file ]`: a truncated or otherwise-corrupt dump fails the replay
#   here, before anything destructive happens to the real database.
#   Returns non-zero (and prints nothing) if the scratch database could not be
#   prepared or the dump did not replay cleanly.
dbs_verify_dump_restores() {
    local container="$1" user="$2" dump="$3" scratch_db="$4"

    if ! docker exec "$container" psql -v ON_ERROR_STOP=1 -U "$user" -d postgres \
            -c "DROP DATABASE IF EXISTS \"$scratch_db\" WITH (FORCE);" >/dev/null 2>&1 \
        || ! docker exec "$container" psql -v ON_ERROR_STOP=1 -U "$user" -d postgres \
            -c "CREATE DATABASE \"$scratch_db\" OWNER \"$user\";" >/dev/null 2>&1; then
        dbs_warn "could not prepare scratch database $scratch_db"
        return 1
    fi

    if ! docker exec -i "$container" psql -v ON_ERROR_STOP=1 --single-transaction \
            -U "$user" "$scratch_db" < "$dump" >/dev/null 2>&1; then
        dbs_warn "dump $dump did not replay cleanly into scratch database $scratch_db"
        return 1
    fi

    local rows
    rows="$(docker exec "$container" psql -tA -U "$user" "$scratch_db" \
        -c "SELECT count(*) FROM media_file;" 2>/dev/null | tr -d '[:space:]')"
    dbs_log "scratch restore of $dump verified: media_file rows=${rows:-0}"
    echo "${rows:-0}"
}

# dbs_scratch_drop CONTAINER USER DB
#   Teardown for a database created by dbs_verify_dump_restores. Best-effort —
#   a leftover scratch database is a nuisance, never a data-loss risk, since it
#   was never anything but a replay target this function itself created.
dbs_scratch_drop() {
    local container="$1" user="$2" db="$3"
    docker exec "$container" psql -v ON_ERROR_STOP=1 -U "$user" -d postgres \
        -c "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE);" >/dev/null 2>&1 || true
}

# The tables a fingerprint covers. Kept as one list so dbs_fingerprint and its
# callers agree on what "the fingerprint" means without repeating the names.
DBS_FINGERPRINT_TABLES=(media_file transcript_segment speaker '"user"' tag alembic_version)

# dbs_fingerprint CONTAINER USER DB OUT_DIR
#   Per-table row count AND a content digest, so a delete+insert pair (same
#   count, different rows) is caught, not just a count. Ordered by the row's
#   own text form rather than an assumed `id` column, since not every table
#   here uses the same PK shape — this only needs to be a STABLE order, not a
#   meaningful one.
dbs_fingerprint() {
    local container="$1" user="$2" db="$3" out_dir="$4"
    mkdir -p "$out_dir"
    local table label
    for table in "${DBS_FINGERPRINT_TABLES[@]}"; do
        label="${table//\"/}"
        docker exec "$container" psql -tA -U "$user" "$db" \
            -c "SELECT count(*) FROM ${table};" 2>/dev/null \
            | tr -d '[:space:]' > "$out_dir/${label}.count" \
            || echo "error" > "$out_dir/${label}.count"
        docker exec "$container" psql -tA -U "$user" "$db" \
            -c "SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM ${table} t;" 2>/dev/null \
            | tr -d '[:space:]' > "$out_dir/${label}.digest" \
            || echo "error" > "$out_dir/${label}.digest"
        # Column list in ordinal order -- lets a LATER comparison (see
        # dbs_digest_baseline_columns) restrict itself to the columns that
        # existed when THIS fingerprint was taken. Without it, a migration
        # that only ADDS columns still moves the whole-row digest above
        # (every row's t::text grows), which reads as data damage when
        # nothing was touched.
        docker exec "$container" psql -tA -U "$user" "$db" \
            -c "SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name='${label}'
                ORDER BY ordinal_position;" 2>/dev/null \
            | tr -d '\r' > "$out_dir/${label}.columns" \
            || : > "$out_dir/${label}.columns"
    done
    dbs_log "fingerprinted $db (${#DBS_FINGERPRINT_TABLES[@]} tables) into $out_dir"
}

# dbs_digest_baseline_columns CONTAINER USER DB TABLE COLUMNS_FILE
#   Content digest of TABLE restricted to the columns named in COLUMNS_FILE
#   (one per line, in that file's order). Exists because dbs_fingerprint above
#   hashes the WHOLE row: a migration that merely ADDS a column rewrites every
#   row's `t::text` and moves the digest without a single stored value
#   changing. Comparing a pre-upgrade fingerprint against a post-upgrade one
#   therefore always "fails" once any column has been added, which is exactly
#   what release-tests/test-upgrade.sh's F-4 assertion was doing.
#   MEASURED: restricting media_file to v0.4.1's 61 columns reproduces the
#   v0.4.1 whole-row digest exactly (01ce171b86eecd9a6a8e0a0830016251) when
#   run against the v0.5.0 database after a full rollback+restore+re-upgrade
#   cycle -- i.e. the data survived byte-for-byte; only the column SET moved.
#   Returns 1 (printing nothing) if any baseline column no longer exists in
#   the current schema -- a DROP/RENAME (e.g. v380 renamed user.keycloak_id)
#   makes the old digest fundamentally unreproducible. That is schema
#   evolution, not damage, and the caller should SKIP rather than fail.
dbs_digest_baseline_columns() {
    local container="$1" user="$2" db="$3" table="$4" columns_file="$5"
    [[ -s "$columns_file" ]] || return 1

    local -a cols=()
    while IFS= read -r col; do
        [[ -n "$col" ]] && cols+=("$col")
    done < "$columns_file"
    [[ ${#cols[@]} -gt 0 ]] || return 1

    local current_cols
    current_cols=$(docker exec "$container" psql -tA -U "$user" "$db" \
        -c "SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='${table}';" 2>/dev/null)
    local col
    for col in "${cols[@]}"; do
        grep -qxF "$col" <<< "$current_cols" || return 1
    done

    local select_list
    select_list=$(printf '"%s",' "${cols[@]}")
    select_list="${select_list%,}"
    docker exec "$container" psql -tA -U "$user" "$db" \
        -c "SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), ''))
            FROM (SELECT ${select_list} FROM ${table}) t;" 2>/dev/null \
        | tr -d '[:space:]'
}

# dbs_table_list CONTAINER USER DB
#   Sorted list of public base-table names. Used to derive "a table introduced
#   by a migration after FROM" as a set difference rather than a hardcoded
#   name — the same discipline ver_alembic_head applies to migration heads.
dbs_table_list() {
    local container="$1" user="$2" db="$3"
    docker exec "$container" psql -tA -U "$user" "$db" \
        -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name;" \
        2>/dev/null | tr -d '\r'
}

# dbs_table_exists CONTAINER USER DB TABLE
dbs_table_exists() {
    local container="$1" user="$2" db="$3" table="$4"
    local exists
    exists="$(docker exec "$container" psql -tA -U "$user" "$db" -c \
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = '${table}');" \
        2>/dev/null | tr -d '[:space:]')"
    [[ "$exists" == "t" ]]
}

# dbs_active_connections CONTAINER USER DB
#   Echoes the count of connections to DB other than this probe's own backend.
dbs_active_connections() {
    local container="$1" user="$2" db="$3"
    docker exec "$container" psql -tA -U "$user" -d postgres -c \
        "SELECT count(*) FROM pg_stat_activity WHERE datname = '${db}' AND pid <> pg_backend_pid();" \
        2>/dev/null | tr -d '[:space:]'
}

# dbs_wait_for_speaker_attributes CONTAINER USER DB TIMEOUT FILE_UUID...
#   Polls until the speaker table has SETTLED for the given media_file uuid(s) — three
#   independent predicates, all of which must hold:
#
#   1. backend/app/tasks/speaker_attribute_task.py's fire-and-forget
#      detect_speaker_attributes_task has finished for every speaker belonging to these
#      files — i.e. every speaker row has attributes_predicted_at IS NOT NULL, the same
#      predicate the app itself uses (_attributes_already_predicted) to decide "already
#      done". A file with zero speaker rows (e.g. single-speaker content the diarizer
#      didn't split, or diarization disabled) is trivially "settled" here — this checks
#      EXISTING rows only, not "at least one row exists".
#   2. No speaker_identification Task row for these files is still 'pending' or
#      'in_progress' — closes issue #617's second write path (LLM speaker-suggestion
#      writes at backend/app/tasks/speaker_identification_task.py:~309-311), which
#      dispatches AFTER the gender-attribute detection above and so can still be running
#      once predicate 1 is already satisfied.
#   3. The speaker-table content digest for these files (same shape as dbs_fingerprint,
#      md5(string_agg(row::text,'|' ORDER BY row::text))) is UNCHANGED across 3
#      consecutive polls ~5s apart — a best-effort settle for any OTHER write this table
#      might still receive that predicates 1/2 don't name, rather than an exhaustive list
#      of every writer.
#
#   Exists to close the race behind issue #617: postprocess.py's
#   _dispatch_speaker_attributes fires the detection task the INSTANT a file's
#   status flips to completed, but does not wait for it — so a caller that
#   snapshots the DB immediately after seeding's "wait for status=completed"
#   loop returns (test-upgrade.sh phase 06b) can catch the speaker table
#   mid-write. That produced a real digest mismatch in a release rehearsal,
#   which without the Fix 1 guard above then killed the whole script under
#   set -e. Issue #620 item 5 (the #617 follow-up) added predicates 2 and 3
#   after finding LLM speaker-suggestion writes could still land after
#   predicate 1 alone was satisfied.
#
#   Returns non-zero (never dies) on timeout so the caller decides whether
#   that is fatal; the digest guard above means it no longer needs to be.
dbs_wait_for_speaker_attributes() {
    local container="$1" user="$2" db="$3" timeout="$4"
    shift 4
    local file_uuids=("$@")
    (( ${#file_uuids[@]} > 0 )) || return 0

    local uuid_list quoted
    quoted=()
    local u
    for u in "${file_uuids[@]}"; do
        quoted+=("'${u}'")
    done
    uuid_list="$(IFS=,; echo "${quoted[*]}")"

    local deadline=$(( $(date +%s) + timeout ))
    local pending="?" pending_llm="?" digest="?" prev_digest="" prev_prev_digest=""
    local stable_streak=0
    while (( $(date +%s) < deadline )); do
        pending="$(docker exec "$container" psql -tA -U "$user" "$db" -c "
            SELECT count(*) FROM speaker s
            JOIN media_file mf ON mf.id = s.media_file_id
            WHERE mf.uuid IN (${uuid_list}) AND s.attributes_predicted_at IS NULL;
        " 2>/dev/null | tr -d '[:space:]')"
        pending_llm="$(docker exec "$container" psql -tA -U "$user" "$db" -c "
            SELECT count(*) FROM task t
            JOIN media_file mf ON mf.id = t.media_file_id
            WHERE mf.uuid IN (${uuid_list}) AND t.task_type = 'speaker_identification'
              AND t.status IN ('pending', 'in_progress');
        " 2>/dev/null | tr -d '[:space:]')"
        digest="$(docker exec "$container" psql -tA -U "$user" "$db" -c "
            SELECT md5(coalesce(string_agg(s::text, '|' ORDER BY s::text), ''))
            FROM speaker s
            JOIN media_file mf ON mf.id = s.media_file_id
            WHERE mf.uuid IN (${uuid_list});
        " 2>/dev/null | tr -d '[:space:]')"

        if [[ "$digest" == "$prev_digest" && "$digest" == "$prev_prev_digest" ]]; then
            stable_streak=3
        else
            stable_streak=1
        fi
        prev_prev_digest="$prev_digest"
        prev_digest="$digest"

        if [[ "$pending" == "0" && "$pending_llm" == "0" && "$stable_streak" -ge 3 ]]; then
            dbs_log "speaker table settled for ${#file_uuids[@]} file(s) (attributes done, no pending speaker_identification task, digest stable across 3 polls)"
            return 0
        fi
        dbs_log "  waiting on speaker table to settle (attrs pending=${pending:-?}, llm task pending=${pending_llm:-?}, digest stable streak=${stable_streak})"
        sleep 5
    done
    dbs_warn "speaker table did not settle within ${timeout}s (attrs pending=${pending:-?}, llm task pending=${pending_llm:-?}) — proceeding anyway; the pre-upgrade snapshot may still race issue #617/#620's window"
    return 1
}

# dbs_wait_for_stable_query CONTAINER USER DB TIMEOUT QUERY_SQL [STABLE_POLLS] [INTERVAL]
#   Generic settle primitive: polls QUERY_SQL (a single-value psql -tA query) until it
#   returns the SAME value STABLE_POLLS times in a row (default 3), INTERVAL seconds apart
#   (default 5) — "stopped changing", not merely "matches an expected value". Used where
#   the set of async post-completion writers touching a table is not exhaustively known (or
#   not worth tracking one predicate per writer, the way dbs_wait_for_speaker_attributes
#   does above) — quiescence is the only signal available in that case.
#
#   Returns non-zero, never dies, on timeout. Prints nothing on stdout — this only gates
#   timing, the caller re-derives whatever it needs (a fingerprint, a fresh dump) afterward.
dbs_wait_for_stable_query() {
    local container="$1" user="$2" db="$3" timeout="$4" query="$5"
    local stable_polls="${6:-3}" interval="${7:-5}"
    local deadline=$(( $(date +%s) + timeout ))
    local last="" current="" streak=0
    while (( $(date +%s) < deadline )); do
        current="$(docker exec "$container" psql -tA -U "$user" "$db" -c "$query" 2>/dev/null | tr -d '[:space:]')"
        if [[ "$current" == "$last" ]]; then
            streak=$(( streak + 1 ))
            if (( streak >= stable_polls )); then
                return 0
            fi
        else
            streak=1
        fi
        last="$current"
        sleep "$interval"
    done
    return 1
}

# dbs_wait_for_media_file_settled CONTAINER USER DB TIMEOUT
#   Waits for media_file's own content digest to stop changing before a caller treats a
#   snapshot of it as stable. This repo has several async post-completion writers that
#   touch media_file at an unpredictable delay after a file's status flips to completed
#   (redaction detection's redaction_status/redaction_model_version/redaction_coverage
#   when redaction is enabled, among others) and none of them is individually tracked the
#   way speaker-attribute detection is above (issue #619, same class as #617's Layer 1).
#   Rather than chase every current and future writer by name, this waits for the table's
#   own content digest to stop moving. Best-effort: proceeding on timeout does not mean
#   settled, it means the caller decided not to wait any longer.
dbs_wait_for_media_file_settled() {
    local container="$1" user="$2" db="$3" timeout="$4"
    if dbs_wait_for_stable_query "$container" "$user" "$db" "$timeout" \
        "SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM media_file t;"; then
        dbs_log "media_file content digest settled"
        return 0
    fi
    dbs_warn "media_file content digest did not settle within ${timeout}s — proceeding anyway; F-4/phase-06b may still race issue #619's window"
    return 1
}

# dbs_wait_for_system_settings_settled CONTAINER USER DB TIMEOUT
#   Same technique, for system_settings. Specifically closes the
#   embedding_normalization_done startup-timer race: app/main.py's
#   _run_one_time_embedding_normalization fires exactly once, ~60s after backend startup,
#   and inserts/updates this table — landing between phase_06b's two backup snapshots
#   (the shipped pg_dump and the ./opentranscribe.sh backup wrapper, taken seconds apart)
#   was issue #619's second reported symptom.
dbs_wait_for_system_settings_settled() {
    local container="$1" user="$2" db="$3" timeout="$4"
    if dbs_wait_for_stable_query "$container" "$user" "$db" "$timeout" \
        "SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM system_settings t;"; then
        dbs_log "system_settings content digest settled"
        return 0
    fi
    dbs_warn "system_settings content digest did not settle within ${timeout}s — proceeding anyway; the backup-content-diff assertion may still race issue #619's window"
    return 1
}

# dbs_diff_fingerprints DIR_A DIR_B LABEL [TABLE...]
#   Compares two fingerprint directories table-by-table via as_record
#   (requires assertions.sh sourced by the caller). With no TABLE arguments,
#   diffs every table present in DIR_A/*.digest — pass an explicit subset when
#   one of the tables is EXPECTED to differ for a reason that is not damage
#   (e.g. alembic_version across a second migration in phase 17).
#   Returns non-zero if any table's digest differs (informational for the
#   caller; as_record has already logged the PASS/FAIL either way).
dbs_diff_fingerprints() {
    local dir_a="$1" dir_b="$2" label="$3"
    shift 3
    local tables=("$@")
    if [[ ${#tables[@]} -eq 0 ]]; then
        local f base
        for f in "$dir_a"/*.digest; do
            [[ -e "$f" ]] || continue
            base="$(basename "$f" .digest)"
            tables+=("$base")
        done
    fi
    local any_fail=0 base digest_a digest_b
    for base in "${tables[@]}"; do
        digest_a="$(cat "$dir_a/${base}.digest" 2>/dev/null || echo '?')"
        digest_b="$(cat "$dir_b/${base}.digest" 2>/dev/null || echo '?')"
        if [[ "$digest_a" == "$digest_b" ]]; then
            as_record PASS "$label: $base content digest unchanged"
        else
            as_record FAIL "$label: $base content digest unchanged" \
                "before=$digest_a after=$digest_b"
            any_fail=1
        fi
    done
    return "$any_fail"
}
