/**
 * Loading the settings modal itself: the thunks the root layout and the prefetchers share.
 *
 * This module is in the root layout's static closure (budget: `scripts/check-bundle-budget.mjs`),
 * so it holds only dynamic-import thunks and must never import `sectionLoaders` or a component
 * statically.
 */
import type { SettingsSection } from '$stores/settingsModalStore';

/** The modal and its sections. Shared so the layout, the prefetchers and the first open make one request. */
export const loadSettingsModal = () => import('$components/SettingsModal.svelte');

/** The lightweight shell painted while `loadSettingsModal` is still in flight. */
export const loadSettingsModalShell = () =>
  import('$components/settings/SettingsModalShell.svelte');

/**
 * Warm the shell, the modal and the section it will land on. Failures are swallowed on purpose:
 * a prefetch that loses the race with the network costs nothing, because opening the modal
 * repeats the import and surfaces the real error there.
 */
export function prefetchSettingsModal(section: SettingsSection = 'system-statistics'): void {
  void loadSettingsModalShell().catch(() => {});
  void loadSettingsModal().catch(() => {});
  void import('$lib/settings/sectionLoaders')
    .then((m) => m.prefetchSection(section))
    .catch(() => {});
}

/** Run `fn` when the browser is idle (Safari has no requestIdleCallback). */
export function whenIdle(fn: () => void): void {
  if (typeof window === 'undefined') return;
  if (window.requestIdleCallback) window.requestIdleCallback(fn, { timeout: 4000 });
  else window.setTimeout(fn, 1500);
}
