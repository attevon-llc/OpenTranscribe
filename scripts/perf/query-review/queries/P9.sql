-- P9: cluster stats / list counts, the repeated scans per request as separate statements.
SELECT (SELECT count(*) FROM speaker_cluster c WHERE c.user_id = (SELECT id FROM "user" WHERE email = 'perf-3@loadtest.invalid')) AS clusters,
       (SELECT count(*) FROM speaker s WHERE s.user_id = (SELECT id FROM "user" WHERE email = 'perf-3@loadtest.invalid') AND s.verified IS FALSE) AS unverified,
       (SELECT max(updated_at) FROM speaker_cluster c WHERE c.user_id = (SELECT id FROM "user" WHERE email = 'perf-3@loadtest.invalid')) AS newest;
