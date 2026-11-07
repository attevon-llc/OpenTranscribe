-- P8: cluster inbox page (services/clustering/verification.py): unverified, unprofiled
-- speakers of a user, outer join cluster, sorted by member_count then confidence.
SELECT s.id, c.member_count, s.confidence
FROM speaker s
LEFT JOIN speaker_cluster_member cm ON cm.speaker_id = s.id
LEFT JOIN speaker_cluster c ON c.id = cm.cluster_id
WHERE s.user_id = (SELECT id FROM "user" WHERE email = 'perf-2@loadtest.invalid')
  AND s.verified IS FALSE AND s.profile_id IS NULL
ORDER BY c.member_count DESC NULLS LAST, s.confidence, s.created_at DESC
LIMIT 20 OFFSET 0;
