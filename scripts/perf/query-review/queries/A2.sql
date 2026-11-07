-- A2: one of the /system/stats scans (throughput by completion day).
SELECT date_trunc('day', completed_at) AS d, count(*) FROM media_file
WHERE completed_at IS NOT NULL AND completed_at > now() - interval '30 days' GROUP BY 1;
