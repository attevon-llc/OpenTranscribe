-- V7: scrub_chat_citations for one destroyed file: a full scan of chat_message with JSONB
-- expansion, once per purged file. Written as the SELECT of the UPDATE's WHERE clause.
SELECT m.id FROM chat_message m
WHERE jsonb_typeof(m.citations) = 'array'
  AND EXISTS (SELECT 1 FROM jsonb_array_elements(m.citations) e
              WHERE e->>'file_uuid' = ANY (ARRAY[md5('perf1337:file:1')::uuid::text]));
