-- Control (G9): hidden-files anti-join on the user_hidden_media primary key.
SELECT count(*) FROM media_file m
WHERE m.user_id = (SELECT id FROM "user" WHERE email = 'perf-2@loadtest.invalid')
  AND NOT EXISTS (SELECT 1 FROM user_hidden_media h
                  WHERE h.user_id = m.user_id AND h.media_file_id = m.id);
