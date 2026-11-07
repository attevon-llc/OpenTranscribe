-- K4: get_recent_tasks (utils/stats_helpers.py): newest tasks, no task.created_at index.
SELECT * FROM task ORDER BY created_at DESC LIMIT 10;
