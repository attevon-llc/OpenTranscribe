/** The hold API an authority uses: the organization's own routes, or the operator's. */
import { AdminHoldsApi, type HoldsApi } from '$lib/api/adminHolds';
import { OrgAdminApi } from '$lib/api/orgAdmin';
import type { HoldPlacer } from '$lib/legalHold/authority';

export function holdsApiFor(placer: HoldPlacer): HoldsApi {
  return placer === 'organization' ? OrgAdminApi : AdminHoldsApi;
}
