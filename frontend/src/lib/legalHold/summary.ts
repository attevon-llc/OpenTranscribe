/**
 * Local updates to a `HoldSummary` after this page placed a hold, or the server refused an edit
 * because of one, so the badge and the frozen controls show without a reload. The next read
 * from the server replaces it; nothing here is the record.
 */
import type { HoldAuthority, HoldSummary } from '$lib/types/hold';

/** `hold` with `by` added to the holders. Never adds hold uuids: those are the server's to give. */
export function markHeld(
  hold: HoldSummary | null | undefined,
  by: readonly HoldAuthority[]
): HoldSummary {
  const heldBy = new Set<HoldAuthority>([...(hold?.held_by ?? []), ...by]);
  return { held: true, held_by: [...heldBy].sort(), hold_uuids: hold?.hold_uuids ?? [] };
}
