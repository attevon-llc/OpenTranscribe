-- T1: trash list page (services/trash_access.py): sort on deleted_at, count over the same set.
SELECT m.id FROM media_file m
WHERE m.deleted_at IS NOT NULL AND m.user_id = (SELECT id FROM "user" WHERE email = 'perf-2@loadtest.invalid')
ORDER BY m.deleted_at DESC, m.id DESC LIMIT 20 OFFSET 0;
