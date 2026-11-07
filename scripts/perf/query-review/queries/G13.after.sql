-- G13 after: NOT EXISTS (anti-join), the proposed rewrite.
SELECT t.id FROM tag t
WHERE NOT EXISTS (SELECT 1 FROM file_tag ft WHERE ft.tag_id = t.id);
