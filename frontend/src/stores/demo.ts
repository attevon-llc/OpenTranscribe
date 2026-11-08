import { derived, get, writable } from 'svelte/store';
import {
  getDemoAttributions,
  getDemoInfo,
  type DemoAttribution,
  type DemoInfo,
} from '$lib/demo/demoApi';

/**
 * Public read-only demo state. `demoMode` is set from `/auth/methods` (`demo_mode`), which the
 * login page already reads; the rest is fetched only once that flag is true, so an ordinary
 * deployment makes no demo request at all.
 */
export const demoMode = writable(false);
export const demoInfo = writable<DemoInfo | null>(null);
export const demoAttributions = writable<Record<string, DemoAttribution>>({});

export const isDemo = derived(demoMode, ($m) => $m);

export async function loadDemoInfo(): Promise<void> {
  try {
    demoInfo.set(await getDemoInfo());
  } catch {
    demoInfo.set(null);
  }
}

/** Credit for every seeded file the signed-in visitor can see; keyed by file uuid. */
export async function loadDemoAttributions(): Promise<void> {
  if (!get(demoMode)) return;
  try {
    const items = await getDemoAttributions();
    demoAttributions.set(Object.fromEntries(items.map((i) => [i.file_uuid, i])));
  } catch {
    demoAttributions.set({});
  }
}
