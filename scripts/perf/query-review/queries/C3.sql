-- Control (P11): identity history served by ix_speaker_identity_event_speaker_created.
SELECT * FROM speaker_identity_event
WHERE speaker_id = (SELECT min(id) FROM speaker)
ORDER BY created_at DESC, id DESC LIMIT 50;
