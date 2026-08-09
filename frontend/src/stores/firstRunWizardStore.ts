import { writable } from 'svelte/store';

/**
 * Cross-component trigger for FirstRunWizard.svelte (HANDOFF #28).
 *
 * The wizard owns its own visibility/step state internally (it only needs to
 * check completion once per session on mount). This store exists solely for
 * the "re-runnable from Settings → Authentication" requirement: a single
 * `requestReopen()` bump the wizard listens for, so Settings does not need to
 * know anything about the wizard's internal state shape.
 */
function createFirstRunWizardStore() {
  const { subscribe, update } = writable({ reopenToken: 0 });

  return {
    subscribe,
    requestReopen: () => update((state) => ({ reopenToken: state.reopenToken + 1 })),
  };
}

export const firstRunWizardStore = createFirstRunWizardStore();
