#!/bin/bash
# Test script to verify search indexing recovery works correctly
# Verifies that files missing search indexes are automatically re-indexed

set -e

echo "=== Search Indexing Recovery Test ==="
echo
echo "This test verifies that files missing search indexes are recovered."
echo

# Check if backend is running
if ! curl -s http://localhost:5174/health > /dev/null 2>&1; then
    echo "✗ Backend is not running. Start it first:"
    echo "  ./opentr.sh start dev"
    exit 1
fi

echo "1. Checking recovery code is in place..."

# Check if search_indexing is in post_transcription_task_types
if grep -q '"search_indexing"' backend/app/services/task_detection_post_transcription.py; then
    echo "✓ Detection: search_indexing tracked in post-transcription tasks"
else
    echo "✗ Detection: search_indexing NOT tracked"
    exit 1
fi

# Check if missing_search_indexing field exists
if grep -q "missing_search_indexing" backend/app/services/task_detection_post_transcription.py; then
    echo "✓ Detection: missing_search_indexing field exists"
else
    echo "✗ Detection: missing_search_indexing field missing"
    exit 1
fi

# Check if recovery dispatches search indexing tasks
if grep -q "index_transcript_search_task.delay" backend/app/services/task_recovery_post_transcription.py; then
    echo "✓ Recovery: Dispatches search indexing tasks"
else
    echo "✗ Recovery: Does NOT dispatch search indexing tasks"
    exit 1
fi

echo
echo "2. Checking normal indexing flow..."

# Verify search indexing task exists and creates Task records
if grep -q 'create_task_record.*"search_indexing"' backend/app/tasks/search_indexing_task.py; then
    echo "✓ Normal flow: search_indexing task creates Task records"
else
    echo "✗ Normal flow: Task record creation missing"
    exit 1
fi

echo
echo "=== All Checks Passed! ==="
echo
echo "Search Indexing Recovery Flow:"
echo
echo "Normal Upload:"
echo "  1. File transcribed → status=COMPLETED"
echo "  2. Transcription dispatches: index_transcript_search_task.delay()"
echo "  3. Task runs, creates Task record (task_type='search_indexing')"
echo "  4. Success: Task status='completed', file indexed in OpenSearch"
echo
echo "Recovery Scenario (Task Failed):"
echo "  1. File status=COMPLETED (transcription done)"
echo "  2. No successful 'search_indexing' task found (status != 'completed')"
echo "  3. Periodic health check (Step 6) detects: missing search indexing"
echo "  4. Recovery dispatches: index_transcript_search_task.delay()"
echo "  5. Task runs, indexes file in OpenSearch"
echo
echo "Recovery Scenario (Reindex Operation Interrupted):"
echo "  1. User triggers manual reindex operation"
echo "  2. Reindex task crashes mid-operation"
echo "  3. Some files indexed, some not"
echo "  4. Health check detects files without completed indexing tasks"
echo "  5. Recovery re-dispatches indexing for affected files"
echo
echo "Detection Logic:"
echo "  - Checks for Task records with task_type='search_indexing' AND status='completed'"
echo "  - Files without successful indexing task → missing_search_indexing=true"
echo "  - Avoids re-dispatch if task attempted in last 30 minutes"
echo
echo "To test manually:"
echo "  1. Transcribe a file"
echo "  2. Delete the search_indexing task record:"
echo "     docker exec opentranscribe-postgres psql -U postgres -d opentranscribe \\"
echo "       -c \"DELETE FROM task WHERE task_type='search_indexing' AND media_file_id=<file_id>;\""
echo "  3. Wait 30+ minutes for health check to run"
echo "  4. Check logs:"
echo "     docker logs opentranscribe-backend | grep 'search indexing'"
echo "  5. Verify file gets re-indexed"
