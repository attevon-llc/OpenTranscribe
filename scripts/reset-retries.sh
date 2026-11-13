#!/bin/bash
# Reset retry counts for all media files
# Usage: ./scripts/reset-retries.sh [file_uuid]
#
# Without arguments: resets all files
# With file_uuid: resets only that specific file

set -e

# shellcheck source=scripts/lib/stack-target.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/stack-target.sh"
st_target || exit $?
CONTAINER="$(st_container postgres)"
DB_USER="postgres"
DB_NAME="opentranscribe"

if [ -n "$1" ]; then
    echo "Resetting retry count for file: $1"
    docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c \
        "UPDATE media_file SET retry_count = 0 WHERE uuid = '$1' RETURNING id, filename, retry_count, max_retries;"
else
    echo "Resetting retry counts for ALL files..."
    docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c \
        "UPDATE media_file SET retry_count = 0; SELECT id, filename, retry_count, max_retries FROM media_file;"
fi

echo "Done!"
