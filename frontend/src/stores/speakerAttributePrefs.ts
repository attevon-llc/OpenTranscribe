/**
 * Whether speaker cards should show predicted attributes (gender badge).
 *
 * Mirrors the user's "Show predictions on speaker cards" setting
 * (`GET /user-settings/speaker-attributes`, `show_attributes_on_cards`). Defaults to
 * visible, and stays visible if the request fails: hiding is an opt-out the user made, never
 * a side effect of an outage. Callers share one in-flight request and a short TTL, so a
 * grid of badges makes one call, and a change saved in Settings shows up on the next badge
 * mount within seconds.
 */

import { writable } from 'svelte/store';
import { getSpeakerAttributeSettings } from '$lib/api/speakerAttributeSettings';

const STALE_AFTER_MS = 15_000;

export const showAttributesOnCards = writable<boolean>(true);

let loadedAt = 0;
let inflight: Promise<void> | null = null;

/** Fetch the preference unless it was loaded recently. `force` bypasses the TTL. */
export function refreshSpeakerAttributePrefs(force = false): Promise<void> {
  if (inflight) return inflight;
  if (!force && Date.now() - loadedAt < STALE_AFTER_MS) return Promise.resolve();
  inflight = getSpeakerAttributeSettings()
    .then((settings) => {
      showAttributesOnCards.set(settings.show_attributes_on_cards !== false);
      loadedAt = Date.now();
    })
    .catch(() => {
      // Keep whatever is shown now; retry on the next mount.
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/** Test seam: forget the cached load so the next refresh fetches. */
export function resetSpeakerAttributePrefs(): void {
  loadedAt = 0;
  inflight = null;
  showAttributesOnCards.set(true);
}
