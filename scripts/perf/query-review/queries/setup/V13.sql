-- Run inside explain.sh's rolled-back transaction before V13: realistic completed_at values.
UPDATE media_file SET completed_at = upload_time + interval '5 min' WHERE deleted_at IS NULL;
