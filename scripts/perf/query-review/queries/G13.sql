-- G13: unused-tag listing, NOT IN (SELECT ...) as written in services/tag_collisions.py.
SELECT t.id FROM tag t
WHERE t.id NOT IN (SELECT ft.tag_id FROM file_tag ft WHERE ft.tag_id IS NOT NULL);
