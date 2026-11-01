import type { MediaRevocation, OrgMediaRevocation } from '$lib/api/admin';

/**
 * True when a quarantine/release response says the file's already-issued media URLs were NOT
 * fully revoked (or restored). The takedown itself always lands; the server decides this
 * (`media_revocation.complete`), the UI only refuses to call it a clean success.
 *
 * A response from a server that predates the field has no `media_revocation` and is treated as
 * complete, which is what it reported before.
 */
export function mediaRevocationIncomplete(result: {
  media_revocation?: MediaRevocation | OrgMediaRevocation | null;
}): boolean {
  return result.media_revocation?.complete === false;
}
