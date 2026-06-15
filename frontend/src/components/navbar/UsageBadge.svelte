<!--
  UsageBadge.svelte — navbar "X / Y hrs" usage chip (cloud edition only).

  Subscribes to the shared usageStore (populated by refreshUsage). Slots into the
  Navbar between notifications and the user dropdown. Renders nothing until usage
  is loaded or when the plan is unmetered (no limit). Color shifts warning/error
  as the quota fills. Clicking opens the usage dashboard in settings.
-->
<script lang="ts">
  import { usageStore, usageFraction } from '$stores/cloudBilling';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import { t } from '$stores/locale';

  const round1 = (n: number) => Math.round(n * 10) / 10;

  $: state = $usageStore;
  $: frac = usageFraction(state);
  // Only org admins can open the usage dashboard; others get a static chip.
  $: canOpenUsage = isCapabilityEnabled($capabilities, 'usage_dashboard');

  $: level = frac === null ? 'ok' : frac >= 1 ? 'over' : frac >= 0.8 ? 'near' : 'ok';

  function openUsage() {
    if (canOpenUsage) settingsModalStore.open('usage');
  }
</script>

{#if state.loaded && state.limit !== null && state.limit > 0}
  <button
    class="usage-badge {level}"
    class:clickable={canOpenUsage}
    on:click={openUsage}
    disabled={!canOpenUsage}
    title={$t('nav.usageBadge.tooltip', { used: round1(state.hours_used), limit: round1(state.limit) })}
    aria-label={$t('nav.usageBadge.tooltip', { used: round1(state.hours_used), limit: round1(state.limit) })}
  >
    <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="10"></circle>
      <polyline points="12 6 12 12 16 14"></polyline>
    </svg>
    <span class="usage-text">{$t('nav.usageBadge.label', { used: round1(state.hours_used), limit: round1(state.limit) })}</span>
  </button>
{/if}

<style>
  .usage-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.35rem 0.6rem;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    background: var(--surface-color);
    color: var(--text-secondary);
    font-size: 0.8rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    cursor: default;
    transition: all 0.2s ease;
    white-space: nowrap;
  }

  .usage-badge.clickable {
    cursor: pointer;
  }

  .usage-badge.clickable:hover {
    background: var(--button-hover);
    color: var(--text-color);
  }

  .usage-badge.near {
    border-color: var(--warning-color, #d97706);
    color: var(--warning-color, #d97706);
  }

  .usage-badge.over {
    border-color: var(--error-color, #dc2626);
    color: var(--error-color, #dc2626);
  }

  @media (max-width: 1024px) {
    .usage-text {
      display: none;
    }
  }
</style>
