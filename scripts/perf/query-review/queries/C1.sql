-- Control: PK-like lookup by uuid. Unaffected by every planned change; if its median moves
-- more than 15% between two runs the pair is noise. Assumes seed id perf1337.
SELECT * FROM media_file WHERE uuid = md5('perf1337:file:1')::uuid;
