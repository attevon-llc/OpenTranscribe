<!--
  QuotaExceededModal.svelte — cloud-edition "out of transcription hours" dialog.

  Shown when an upload pre-flight check or an HTTP 402 from the backend signals
  the org has hit its plan quota. Reuses the shared BaseModal. The upgrade CTA
  routes to the billing settings panel (org admins) so they can start a Checkout
  / open the Customer Portal.

  Driven by the global `quotaModal` store so the axios interceptor (which owns no
  component) can pop it. Mounted once in the app shell, cloud edition only.
-->
<script lang="ts">
  import BaseModal from '$components/ui/BaseModal.svelte';
  import { quotaModal, dismissQuotaExceeded } from '$stores/quotaModal';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import { t } from '$stores/locale';
  import { usageStore } from '$stores/cloudBilling';

  $: isOpen = $quotaModal.open;
  $: detail = $quotaModal.message;

  // Only org admins can act on billing; everyone else just gets the explanation.
  $: canManageBilling = isCapabilityEnabled($capabilities, 'billing');

  function handleClose() {
    dismissQuotaExceeded();
  }

  function handleUpgrade() {
    dismissQuotaExceeded();
    settingsModalStore.open('billing');
  }
</script>

<BaseModal {isOpen} title={$t('cloud.quota.title')} onClose={handleClose} maxWidth="480px">
  <div class="quota-body">
    <div class="quota-icon" aria-hidden="true">
      <svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>
        <line x1="12" y1="9" x2="12" y2="13"></line>
        <line x1="12" y1="17" x2="12.01" y2="17"></line>
      </svg>
    </div>
    <p class="quota-message">{detail || $t('cloud.quota.message')}</p>

    {#if $usageStore.loaded && $usageStore.limit}
      <p class="quota-usage">
        {$t('cloud.quota.usageLine', {
          used: Math.round($usageStore.hours_used * 10) / 10,
          limit: Math.round($usageStore.limit * 10) / 10,
        })}
      </p>
    {/if}

    {#if canManageBilling}
      <p class="quota-hint">{$t('cloud.quota.upgradeHint')}</p>
    {:else}
      <p class="quota-hint">{$t('cloud.quota.contactAdminHint')}</p>
    {/if}
  </div>

  <svelte:fragment slot="footer">
    <button type="button" class="btn btn-secondary" on:click={handleClose}>
      {$t('cloud.quota.dismiss')}
    </button>
    {#if canManageBilling}
      <button type="button" class="btn btn-primary" on:click={handleUpgrade}>
        {$t('cloud.quota.upgrade')}
      </button>
    {/if}
  </svelte:fragment>
</BaseModal>

<style>
  .quota-body {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    gap: 0.75rem;
  }

  .quota-icon {
    color: var(--warning-color, #d97706);
  }

  .quota-message {
    margin: 0;
    font-size: 0.95rem;
    color: var(--text-color);
    line-height: 1.5;
  }

  .quota-usage {
    margin: 0;
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-secondary);
  }

  .quota-hint {
    margin: 0;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .btn {
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    border: none;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-primary {
    background-color: var(--primary-color);
    color: white;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-primary:hover {
    background-color: var(--primary-hover);
    transform: translateY(-1px);
  }

  .btn-secondary {
    background-color: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
  }

  .btn-secondary:hover {
    background-color: var(--button-hover);
  }
</style>
