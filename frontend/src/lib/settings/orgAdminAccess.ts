/**
 * Whether an organization-administrator surface is offered (managed edition). One predicate
 * for the Settings sidebar and every other place that gates on it (the legal-hold actions in
 * the gallery and on the file page), so the two can never disagree about who is an org admin.
 */
import { isCapabilityEnabled, type CapabilitiesState } from '$stores/capabilities';

/**
 * True when the build is the managed edition, the backend enabled `key` for this request and
 * aimed it at organization administrators. The community edition has no organizations, so this
 * is always false there. The server enforces the same on every route; this only keeps a control
 * from advertising an action that would refuse.
 */
export function orgAdminCapOn(
  state: CapabilitiesState,
  key: string,
  isCloudEdition: boolean
): boolean {
  return isCloudEdition && isCapabilityEnabled(state, key) && state.audience[key] === 'org_admin';
}
