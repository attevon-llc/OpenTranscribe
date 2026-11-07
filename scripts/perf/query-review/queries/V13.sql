-- V13: retention candidate query (tasks/cleanup.py); a read, so no completed_at rewrite needed.
SELECT m.id FROM media_file m
WHERE (m.completed_at IS NOT NULL AND m.completed_at < now() - interval '365 days')
   OR (m.completed_at IS NULL AND m.upload_time < now() - interval '365 days');
