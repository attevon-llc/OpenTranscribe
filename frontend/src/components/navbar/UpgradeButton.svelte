<!--
  UpgradeButton.svelte — navbar upgrade CTA (cloud edition only).

  Shown only when usage is at/over the plan limit threshold (isNearLimit) AND the
  current user can manage billing (cap:billing → org admin). Clicking opens the
  billing settings panel. Subscribes to the shared usageStore.
-->
<script lang="ts">
  import { usageStore, isNearLimit, isOverLimit } from '$stores/cloudBilling';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import { t } from '$stores/locale';

  $: state = $usageStore;
  $: canManageBilling = isCapabilityEnabled($capabilities, 'billing');
  $: shouldShow = state.loaded && isNearLimit(state) && canManageBilling;
  $: over = isOverLimit(state);

  function openBilling() {
    settingsModalStore.open('billing');
  }
</script>

{#if shouldShow}
  <button
    class="upgrade-button"
    class:over
    on:click={openBilling}
    title={$t('nav.upgrade.tooltip')}
    aria-label={$t('nav.upgrade.label')}
  >
    <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="18 15 12 9 6 15"></polyline>
    </svg>
    <span class="upgrade-text">{$t('nav.upgrade.label')}</span>
  </button>
{/if}

<style>
  .upgrade-button {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.35rem 0.7rem;
    border-radius: 8px;
    border: none;
    background: var(--primary-color);
    color: white;
    font-size: 0.8rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s ease;
    white-space: nowrap;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .upgrade-button:hover {
    background: var(--primary-hover);
    transform: translateY(-1px);
  }

  .upgrade-button.over {
    background: var(--error-color, #dc2626);
    box-shadow: 0 2px 4px rgba(var(--error-color-rgb, 239, 68, 68), 0.25);
  }

  @media (max-width: 1024px) {
    .upgrade-text {
      display: none;
    }
  }
</style>
