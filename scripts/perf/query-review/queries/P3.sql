-- P3 / #1328: the privacy scope pre-query. The inner subquery matches the v458 partial index
-- (ix_speaker_machine_identity_media_file) predicate. Measure with and without that index.
SELECT DISTINCT m.user_id, m.organization_id FROM media_file m
WHERE m.id IN (SELECT id FROM media_file WHERE user_id = (SELECT id FROM "user" WHERE email = 'perf-2@loadtest.invalid'))
  AND m.id IN (SELECT s.media_file_id FROM speaker s
               WHERE s.identity_source IN ('voiceprint', 'voiceprint_link'));
