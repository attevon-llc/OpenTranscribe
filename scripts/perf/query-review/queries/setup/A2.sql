-- Run inside explain.sh's rolled-back transaction before A2: the seed leaves completed_at NULL
-- and every upload_time older than A2's 30-day window, so spread completions over 30 days.
UPDATE media_file SET completed_at = now() - (id % 30) * interval '1 day' WHERE deleted_at IS NULL;
