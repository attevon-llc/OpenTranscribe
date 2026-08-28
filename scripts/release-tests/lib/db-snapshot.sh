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
    done
    dbs_log "fingerprinted $db (${#DBS_FINGERPRINT_TABLES[@]} tables) into $out_dir"
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
#   Polls until backend/app/tasks/speaker_attribute_task.py's fire-and-forget
#   detect_speaker_attributes_task has settled for every speaker belonging to
#   the given media_file uuid(s) — i.e. every speaker row for those files has
#   attributes_predicted_at IS NOT NULL, the same predicate the app itself uses
#   (_attributes_already_predicted) to decide "already done".
#
#   Exists to close the race behind issue #617: postprocess.py's
#   _dispatch_speaker_attributes fires the detection task the INSTANT a file's
#   status flips to completed, but does not wait for it — so a caller that
#   snapshots the DB immediately after seeding's "wait for status=completed"
#   loop returns (test-upgrade.sh phase 06b) can catch the speaker table
#   mid-write. That produced a real digest mismatch in a release rehearsal,
#   which without the Fix 1 guard above then killed the whole script under
#   set -e.
#
#   A file with zero speaker rows (e.g. single-speaker content the diarizer
#   didn't split, or diarization disabled) is trivially "settled" and never
#   waited on — this checks EXISTING rows only, not "at least one row exists".
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
    local pending="?"
    while (( $(date +%s) < deadline )); do
        pending="$(docker exec "$container" psql -tA -U "$user" "$db" -c "
            SELECT count(*) FROM speaker s
            JOIN media_file mf ON mf.id = s.media_file_id
            WHERE mf.uuid IN (${uuid_list}) AND s.attributes_predicted_at IS NULL;
        " 2>/dev/null | tr -d '[:space:]')"
        if [[ "$pending" == "0" ]]; then
            dbs_log "speaker attribute detection settled for ${#file_uuids[@]} file(s)"
            return 0
        fi
        dbs_log "  waiting on speaker attribute detection (${pending:-?} speaker row(s) still pending)"
        sleep 5
    done
    dbs_warn "speaker attribute detection did not settle within ${timeout}s (pending=${pending:-?}) — proceeding anyway; the pre-upgrade snapshot may still race issue #617's window"
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
