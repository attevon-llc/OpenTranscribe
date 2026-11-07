-- T4: admin/org hold list page. The page query is batched; the N+1 is the per-hold collection
-- lookup, so replay the page then the per-hold probe shape (one hold shown).
SELECT h.id, h.authority, h.organization_id, h.released_at,
       (SELECT count(*) FROM content_hold_file f WHERE f.hold_id = h.id) AS files
FROM content_hold h ORDER BY h.placed_at DESC, h.id DESC LIMIT 200;
