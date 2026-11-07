-- S1 / G1: collection ranks for one user. Approximates collection_permission_ranks: a correlated
-- scalar subquery per collection row (org role) greatest'd with the share rank; scans EVERY
-- collection in the install, then keeps rank > 0.
SELECT c.id, GREATEST(
         COALESCE((SELECT CASE m.role WHEN 'org:admin' THEN 3 ELSE CASE c.visibility
                           WHEN 'organization' THEN 1 ELSE 0 END END
                   FROM organization_membership m
                   WHERE m.organization_id = c.organization_id AND m.user_id = (SELECT id FROM "user" WHERE email = 'perf-3@loadtest.invalid')), 0),
         COALESCE((SELECT 2 FROM collection_share s
                   WHERE s.collection_id = c.id AND s.target_user_id = (SELECT id FROM "user" WHERE email = 'perf-3@loadtest.invalid') LIMIT 1), 0)) AS rank
FROM collection c
WHERE GREATEST(
         COALESCE((SELECT 1 FROM organization_membership m
                   WHERE m.organization_id = c.organization_id AND m.user_id = (SELECT id FROM "user" WHERE email = 'perf-3@loadtest.invalid')), 0),
         COALESCE((SELECT 2 FROM collection_share s
                   WHERE s.collection_id = c.id AND s.target_user_id = (SELECT id FROM "user" WHERE email = 'perf-3@loadtest.invalid') LIMIT 1), 0)) > 0;
