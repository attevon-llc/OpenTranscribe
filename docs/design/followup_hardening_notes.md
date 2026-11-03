# Follow-up hardening notes (issue #1357)

Items from the storage, purge and audit review that need a design decision before code. Each is
a failure description and a proposed fix; none is implemented.

## Re-import race in `_drop_shared_paths`

`file_cleanup_service._drop_shared_paths` decides, inside the purge transaction, that no other
media row references a key, and the object store delete happens later with no session open. If a
user re-imports the same source video between the check and the delete, the new row names the
same thumbnail key, the purge then deletes it (and, on a versioned bucket, every version), and
the new row is left pointing at nothing. Proposed fix: make the delete conditional on a second
reference check immediately before each object delete (a short read session, key still
unreferenced), and treat "referenced again" as keep. For URL imports, also consider writing the
thumbnail under a per-row key so rows never share one; that removes the class rather than
narrowing the window.

## Concurrent purges of rows that share a storage key

Two purges of rows that share a key each see the other row still present when they run
`_drop_shared_paths`, so each keeps the key, and when both finish nobody deletes it: the object
leaks (the last-row-deletes rule needs the last row to observe an empty reference set, but the
two checks overlap). Proposed fix: take a transaction-scoped advisory lock keyed on a hash of the
storage key around the check and the row delete, so the second purge sees the first one's delete
committed; alternatively let the orphan sweep reclaim the key, which already handles unreferenced
keys, and document that as the backstop.

## Concurrent avatar uploads

Two avatar uploads for the same user race on the read-old-path, write-new-object, update-row,
delete-old-object sequence: both read the same old path, both delete it, and whichever updates
the row last wins while the other's newly written object is left unreferenced (or, with a
reused key, one upload overwrites the other's bytes). Proposed fix: write each upload under a
unique key, update the row with a compare-and-set on the old path (`UPDATE ... WHERE
avatar_path = :old`), and delete the old object only when the update matched; the loser deletes
its own new object. The orphan sweep covers any residue from a crash between the steps.
