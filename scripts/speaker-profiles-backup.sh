#!/usr/bin/env bash
# Speaker Profile Backup & Restore
#
# Exports speaker profiles (names + voice embeddings) from PostgreSQL and OpenSearch
# so they can be restored after a data reset. This preserves cross-file speaker
# identification capabilities.
#
# Usage:
#   ./scripts/speaker-profiles-backup.sh backup [output_dir]
#   ./scripts/speaker-profiles-backup.sh restore [backup_dir]
#   ./scripts/speaker-profiles-backup.sh list
#
# Default backup directory: ./backups/speaker-profiles/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Resolve the stack by compose labels and observed ports; credentials come from env_reader
# (python-dotenv), never `source .env`.
# shellcheck source=scripts/lib/stack-target.sh
source "$SCRIPT_DIR/lib/stack-target.sh"
st_target || exit $?
_env_value() {
    python3 "$SCRIPT_DIR/lib/env_reader.py" "$PROJECT_DIR/.env" "$1" --default "${2:-}"
}
OPENSEARCH_URL="http://localhost:${OPENSEARCH_PORT}"
PG_CONTAINER="$(st_container postgres)"
PG_USER="${POSTGRES_USER:-$(_env_value POSTGRES_USER postgres)}"
PG_DB="${POSTGRES_DB:-$(_env_value POSTGRES_DB opentranscribe)}"

DEFAULT_BACKUP_DIR="$PROJECT_DIR/backups/speaker-profiles"

usage() {
    echo "Usage: $0 {backup|restore|list} [directory]"
    echo ""
    echo "Commands:"
    echo "  backup [dir]   Export speaker profiles to directory (default: $DEFAULT_BACKUP_DIR)"
    echo "  restore [dir]  Import speaker profiles from backup directory"
    echo "  list           Show current speaker profiles in database"
    exit 1
}

check_services() {
    if ! curl -s "$OPENSEARCH_URL" > /dev/null 2>&1; then
        echo "ERROR: OpenSearch not reachable at $OPENSEARCH_URL"
        exit 1
    fi
    if ! docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" > /dev/null 2>&1; then
        echo "ERROR: PostgreSQL container '$PG_CONTAINER' not reachable"
        exit 1
    fi
}

do_backup() {
    local backup_dir="${1:-$DEFAULT_BACKUP_DIR}"
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)
    backup_dir="$backup_dir/$timestamp"

    echo "=== Speaker Profile Backup ==="
    check_services

    mkdir -p "$backup_dir"

    # 1. Export speaker profiles from PostgreSQL
    echo "Exporting speaker profiles from PostgreSQL..."
    docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -t -A -F'|' -c \
        "SELECT uuid, name, description, embedding_count, last_embedding_update, created_at, updated_at FROM speaker_profile ORDER BY name;" \
        > "$backup_dir/profiles_pg.csv"

    local profile_count
    profile_count=$(wc -l < "$backup_dir/profiles_pg.csv" | tr -d ' ')
    echo "  Found $profile_count profiles in PostgreSQL"

    # 2. Export profile embeddings from OpenSearch
    echo "Exporting profile embeddings from OpenSearch..."
    curl -s "$OPENSEARCH_URL/speakers/_search" \
        -H 'Content-Type: application/json' \
        -d '{
            "query": {"term": {"document_type": "profile"}},
            "size": 1000,
            "_source": true
        }' | python3 -c "
import sys, json
data = json.load(sys.stdin)
hits = data.get('hits', {}).get('hits', [])
profiles = []
for hit in hits:
    src = hit['_source']
    profiles.append({
        'doc_id': hit['_id'],
        'profile_uuid': src.get('profile_uuid'),
        'profile_name': src.get('profile_name'),
        'profile_id': src.get('profile_id'),
        'user_id': src.get('user_id'),
        'embedding': src.get('embedding'),
        'speaker_count': src.get('speaker_count'),
        'embedding_count': src.get('embedding_count'),
        'document_type': src.get('document_type'),
        'updated_at': src.get('updated_at'),
    })
json.dump(profiles, sys.stdout, indent=2)
print()
" > "$backup_dir/profiles_opensearch.json"

    local os_count
    os_count=$(python3 -c "import json; print(len(json.load(open('$backup_dir/profiles_opensearch.json'))))")
    echo "  Found $os_count profile embeddings in OpenSearch"

    # 3. Export index mapping for dimension info
    echo "Exporting OpenSearch index mapping..."
    curl -s "$OPENSEARCH_URL/speakers/_mapping" > "$backup_dir/speakers_mapping.json"

    # 4. Save metadata
    cat > "$backup_dir/backup_metadata.json" <<EOF
{
    "timestamp": "$timestamp",
    "pg_profile_count": $profile_count,
    "os_profile_count": $os_count,
    "opensearch_url": "$OPENSEARCH_URL",
    "embedding_dimension": $(python3 -c "import json; m=json.load(open('$backup_dir/speakers_mapping.json')); print(m.get('speakers',{}).get('mappings',{}).get('properties',{}).get('embedding',{}).get('dimension','unknown'))")
}
EOF

    echo ""
    echo "Backup complete: $backup_dir"
    echo "Files:"
    ls -lh "$backup_dir"
    echo ""
    echo "Profiles saved:"
    while IFS='|' read -r uuid name desc count last_update created updated; do
        printf "  %-25s (embeddings: %s)\n" "$name" "$count"
    done < "$backup_dir/profiles_pg.csv"
}

do_restore() {
    local backup_dir="${1:-}"

    if [[ -z "$backup_dir" ]]; then
        # Find latest backup
        local base_dir="${DEFAULT_BACKUP_DIR}"
        if [[ ! -d "$base_dir" ]]; then
            echo "ERROR: No backup directory found at $base_dir"
            exit 1
        fi
        backup_dir=$(find "$base_dir" -maxdepth 1 -type d | sort | tail -1)
        if [[ "$backup_dir" == "$base_dir" ]]; then
            echo "ERROR: No backups found in $base_dir"
            exit 1
        fi
    fi

    if [[ ! -f "$backup_dir/profiles_pg.csv" ]] || [[ ! -f "$backup_dir/profiles_opensearch.json" ]]; then
        echo "ERROR: Missing backup files in $backup_dir"
        echo "Expected: profiles_pg.csv, profiles_opensearch.json"
        exit 1
    fi

    echo "=== Speaker Profile Restore ==="
    echo "Restoring from: $backup_dir"
    check_services

    local meta_file="$backup_dir/backup_metadata.json"
    if [[ -f "$meta_file" ]]; then
        echo "Backup metadata:"
        python3 -c "import json; m=json.load(open('$meta_file')); print(f'  Timestamp: {m[\"timestamp\"]}'); print(f'  Profiles: {m[\"pg_profile_count\"]}'); print(f'  Embeddings: {m[\"os_profile_count\"]}'); print(f'  Dimension: {m[\"embedding_dimension\"]}')"
    fi

    # 1. Restore speaker profiles to PostgreSQL
    echo ""
    echo "Restoring speaker profiles to PostgreSQL..."
    local restored=0
    local skipped=0
    while IFS='|' read -r uuid name desc count last_update created updated; do
        [[ -z "$uuid" ]] && continue
        # Check if profile already exists
        local exists
        exists=$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -t -A -c \
            "SELECT count(*) FROM speaker_profile WHERE uuid='$uuid';")
        if [[ "$exists" -gt 0 ]]; then
            skipped=$((skipped + 1))
            continue
        fi
        # Insert profile
        docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -c \
            "INSERT INTO speaker_profile (uuid, user_id, name, description, embedding_count, last_embedding_update, created_at, updated_at)
             VALUES ('$uuid', 1, '$(echo "$name" | sed "s/'/''/g")', '$(echo "$desc" | sed "s/'/''/g")', $count,
                     $(if [[ -n "$last_update" && "$last_update" != "" ]]; then echo "'$last_update'"; else echo "NULL"; fi),
                     '$created',
                     $(if [[ -n "$updated" && "$updated" != "" ]]; then echo "'$updated'"; else echo "'$created'"; fi));" \
            > /dev/null 2>&1
        restored=$((restored + 1))
    done < "$backup_dir/profiles_pg.csv"
    echo "  Restored: $restored, Skipped (already exist): $skipped"

    # 2. Ensure OpenSearch speakers index exists with correct mapping
    echo "Checking OpenSearch speakers index..."
    if ! curl -s "$OPENSEARCH_URL/speakers" > /dev/null 2>&1 || \
       [[ $(curl -s -o /dev/null -w "%{http_code}" "$OPENSEARCH_URL/speakers") == "404" ]]; then
        echo "  Creating speakers index from backup mapping..."
        if [[ -f "$backup_dir/speakers_mapping.json" ]]; then
            local mapping
            mapping=$(python3 -c "
import json
m = json.load(open('$backup_dir/speakers_mapping.json'))
props = m.get('speakers', {}).get('mappings', {}).get('properties', {})
print(json.dumps({'mappings': {'properties': props}}))" )
            curl -s -X PUT "$OPENSEARCH_URL/speakers" \
                -H 'Content-Type: application/json' \
                -d "$mapping" > /dev/null
            echo "  Index created."
        else
            echo "  WARNING: No mapping backup found. Index must be created by backend startup."
        fi
    else
        echo "  Index already exists."
    fi

    # 3. Restore profile embeddings to OpenSearch
    echo "Restoring profile embeddings to OpenSearch..."
    local os_restored=0
    local os_skipped=0
    python3 -c "
import json, sys
profiles = json.load(open('$backup_dir/profiles_opensearch.json'))
for p in profiles:
    doc_id = p.get('doc_id', f'profile_{p[\"profile_uuid\"]}')
    # Build the document without doc_id
    doc = {k: v for k, v in p.items() if k != 'doc_id'}
    print(json.dumps({'doc_id': doc_id, 'doc': doc}))
" | while IFS= read -r line; do
        local doc_id doc
        doc_id=$(echo "$line" | python3 -c "import sys,json; print(json.load(sys.stdin)['doc_id'])")
        doc=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['doc']))")

        # Check if already exists
        local status
        status=$(curl -s -o /dev/null -w "%{http_code}" "$OPENSEARCH_URL/speakers/_doc/$doc_id")
        if [[ "$status" == "200" ]]; then
            os_skipped=$((os_skipped + 1))
            echo "  Skipped (exists): $doc_id"
            continue
        fi

        curl -s -X PUT "$OPENSEARCH_URL/speakers/_doc/$doc_id" \
            -H 'Content-Type: application/json' \
            -d "$doc" > /dev/null
        os_restored=$((os_restored + 1))
        local profile_name
        profile_name=$(echo "$doc" | python3 -c "import sys,json; print(json.load(sys.stdin).get('profile_name','unknown'))")
        echo "  Restored: $profile_name ($doc_id)"
    done

    echo ""
    echo "Restore complete!"
    echo ""
    echo "Current profiles in database:"
    docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -c \
        "SELECT name, embedding_count, created_at::date FROM speaker_profile ORDER BY name;"
}

do_list() {
    check_services
    echo "=== Current Speaker Profiles ==="
    echo ""
    echo "PostgreSQL:"
    docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -c \
        "SELECT name, embedding_count, created_at::date as created FROM speaker_profile ORDER BY name;"

    echo ""
    echo "OpenSearch profile documents:"
    curl -s "$OPENSEARCH_URL/speakers/_search" \
        -H 'Content-Type: application/json' \
        -d '{"query":{"term":{"document_type":"profile"}},"size":100,"_source":["profile_name","profile_uuid","speaker_count","embedding_count"]}' \
        | python3 -c "
import sys, json
data = json.load(sys.stdin)
hits = data.get('hits', {}).get('hits', [])
if not hits:
    print('  No profile documents found')
else:
    for h in sorted(hits, key=lambda x: x['_source'].get('profile_name', '')):
        s = h['_source']
        print(f'  {s.get(\"profile_name\", \"unknown\"):<25} uuid={s.get(\"profile_uuid\", \"?\")[:8]}... embeddings={s.get(\"embedding_count\", s.get(\"speaker_count\", \"?\"))}')
"
}

case "${1:-}" in
    backup)
        do_backup "${2:-}"
        ;;
    restore)
        do_restore "${2:-}"
        ;;
    list)
        do_list
        ;;
    *)
        usage
        ;;
esac
