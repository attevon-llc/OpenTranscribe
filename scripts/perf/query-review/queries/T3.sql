-- T3: purge selector (services/trash_purge.py), ordered by the ix_media_file_trash_due index.
SELECT m.id, m.uuid FROM media_file m
WHERE m.deleted_at IS NOT NULL AND m.purge_after <= clock_timestamp()
  AND m.legal_hold IS NOT TRUE
  AND NOT EXISTS (SELECT 1 FROM content_hold_file f JOIN content_hold h ON h.id = f.hold_id
                  WHERE f.media_file_id = m.id AND h.released_at IS NULL)
  AND m.is_quarantined IS FALSE
ORDER BY m.purge_after, m.id LIMIT 200;
