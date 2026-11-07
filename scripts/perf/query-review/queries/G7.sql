-- G7: held=true filter. hold_guard.held_pred(): legal_hold OR EXISTS(active hold).
SELECT m.id FROM media_file m
WHERE m.user_id = (SELECT id FROM "user" WHERE email = 'perf-2@loadtest.invalid')
  AND (m.legal_hold IS TRUE
       OR EXISTS (SELECT 1 FROM content_hold_file f JOIN content_hold h ON h.id = f.hold_id
                  WHERE f.media_file_id = m.id AND h.released_at IS NULL))
ORDER BY m.upload_time DESC LIMIT 20;
