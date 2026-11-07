-- G8: held_count in /files/metadata-filters (EXISTS under OR inside an aggregate FILTER).
SELECT count(m.id) FILTER (WHERE m.legal_hold IS TRUE
         OR EXISTS (SELECT 1 FROM content_hold_file f JOIN content_hold h ON h.id = f.hold_id
                    WHERE f.media_file_id = m.id AND h.released_at IS NULL)) AS held_count,
       count(m.id) AS total
FROM media_file m WHERE m.user_id = (SELECT id FROM "user" WHERE email = 'perf-2@loadtest.invalid') AND m.deleted_at IS NULL;
