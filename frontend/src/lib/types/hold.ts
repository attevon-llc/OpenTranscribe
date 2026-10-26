/**
 * Who holds a file or collection against deletion and reprocessing (issue #1276), as the server
 * reports it on every file and collection response. Never a reason: that is the holding
 * organization's administrators' alone (`OrgHold`, `$lib/api/orgAdmin`).
 */
export type HoldAuthority = 'platform' | 'organization';

export interface HoldSummary {
  held: boolean;
  /** Who holds it. Empty when not held. */
  held_by: HoldAuthority[];
  /** The organization's hold ids, present only for an administrator of that organization. */
  hold_uuids: string[];
}
