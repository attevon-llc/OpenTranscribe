#!/bin/bash
# Comprehensive diagnostic for file integrity issues
# Checks for: transcripts without videos, ERROR files with transcripts, stuck PROCESSING, etc.

set -e

# shellcheck source=scripts/lib/stack-target.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/stack-target.sh"
st_target || exit $?

echo "=== File Integrity Diagnostic ==="
echo

# 1. Files stuck in PROCESSING with transcripts (transcription done, but status not updated)
echo "1. Checking PROCESSING files with transcripts (should be COMPLETED)..."
docker exec "$(st_container postgres)" psql -U postgres -d opentranscribe -t -c "
SELECT
    mf.id,
    mf.filename,
    mf.completed_at,
    COUNT(ts.id) as segments
FROM media_file mf
JOIN transcript_segment ts ON ts.media_file_id = mf.id
WHERE mf.status = 'processing'
GROUP BY mf.id, mf.filename, mf.completed_at;
" | while read -r line; do
    if [ -n "$line" ]; then
        echo "  ⚠ STUCK: $line"
    fi
done

# 2. Files with transcripts but ERROR status
echo
echo "2. Checking ERROR files with transcripts (transcription succeeded but marked ERROR)..."
ERRORCOUNT=$(docker exec "$(st_container postgres)" psql -U postgres -d opentranscribe -t -c "
SELECT COUNT(DISTINCT mf.id)
FROM media_file mf
WHERE mf.status = 'error'
AND EXISTS(SELECT 1 FROM transcript_segment WHERE media_file_id = mf.id LIMIT 1);
" | tr -d ' ')

echo "  Found: $ERRORCOUNT ERROR files with transcripts"

if [ "$ERRORCOUNT" -gt "0" ]; then
    echo "  Sample files:"
    docker exec "$(st_container postgres)" psql -U postgres -d opentranscribe -c "
    SELECT
        mf.id,
        mf.filename,
        mf.last_error_message,
        (SELECT COUNT(*) FROM transcript_segment WHERE media_file_id = mf.id) as segments
    FROM media_file mf
    WHERE mf.status = 'error'
    AND EXISTS(SELECT 1 FROM transcript_segment WHERE media_file_id = mf.id)
    LIMIT 5;
    "
fi

# 3. Files with storage_path but missing from MinIO
echo
echo "3. Checking for video files that might be missing from MinIO..."
echo "  (This requires checking MinIO directly - cannot be done from SQL alone)"
echo "  Run: docker exec $(st_container backend) python -c \"
from app.services.minio_service import minio_client
from app.core.config import settings
objects = list(minio_client.list_objects(settings.MEDIA_BUCKET_NAME, recursive=True))
print(f'Total objects in MinIO: {len(objects)}')
\""

# 4. Summary statistics
echo
echo "4. Overall Statistics:"
docker exec "$(st_container postgres)" psql -U postgres -d opentranscribe -c "
SELECT
    status,
    COUNT(*) as files,
    COUNT(CASE WHEN storage_path IS NOT NULL AND storage_path != '' THEN 1 END) as with_video,
    SUM((SELECT COUNT(*) FROM transcript_segment WHERE media_file_id = mf.id)) as total_segments
FROM media_file mf
GROUP BY status
ORDER BY status;
"

echo
echo "=== Diagnostic Complete ==="
echo
echo "Common Issues Found:"
echo "  - PROCESSING files with transcripts: Should be marked COMPLETED"
echo "  - ERROR files with transcripts: Transcription succeeded but post-processing failed"
echo
echo "Recommended Actions:"
echo "  1. Run periodic health check to recover stuck files"
echo "  2. Check last_error_message for ERROR files to understand failure"
echo "  3. Consider manual status correction for files with complete transcripts"
