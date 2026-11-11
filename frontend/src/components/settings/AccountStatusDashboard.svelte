<script lang="ts">
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { onMount } from 'svelte';
  import { AdminApi, type AccountStatusReport } from '$lib/api/admin';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';

  let report: AccountStatusReport | null = null;
  let loading = false;

  onMount(async () => {
    await loadReport();
  });

  async function loadReport() {
    loading = true;
    try {
      report = await AdminApi.getAccountStatusReport();
    } catch (error) {
      console.error('Failed to load account status report:', error);
      toastStore.error($t('settings.accountStatus.loadError'));
    } finally {
      loading = false;
    }
  }

</script>

<div class="account-status-strip">
  {#if loading}
    <SettingsSkeleton variant="chips" />
  {:else if report}
    <div class="status-chips">
      <span class="chip">
        <span class="chip-value">{report.total_users}</span>
        <span class="chip-label">{$t('settings.accountStatus.totalUsers')}</span>
      </span>
      <span class="chip success">
        <span class="chip-value">{report.active_users}</span>
        <span class="chip-label">{$t('settings.accountStatus.activeUsers')}</span>
      </span>
      <span class="chip warning">
        <span class="chip-value">{report.inactive_users}</span>
        <span class="chip-label">{$t('settings.accountStatus.inactiveUsers')}</span>
      </span>
      <span class="chip info">
        <span class="chip-value">{report.mfa_enabled_users}</span>
        <span class="chip-label">{$t('settings.accountStatus.mfaEnabled')}</span>
      </span>
      {#if report.password_expired_users > 0}
        <span class="chip danger">
          <span class="chip-value">{report.password_expired_users}</span>
          <span class="chip-label">{$t('settings.accountStatus.passwordExpired')}</span>
        </span>
      {/if}
    </div>
  {/if}
</div>

<style>
  .account-status-strip {
    margin-bottom: var(--space-5);
    padding: var(--space-3) var(--space-4);
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-xl);
  }

  .status-chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    align-items: center;
    justify-content: center;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
    padding: var(--space-1-5) var(--space-3);
    border-radius: var(--radius-pill);
    font-size: 0.8rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .chip-value {
    font-weight: var(--font-weight-semibold);
    font-size: 0.85rem;
  }

  .chip-label {
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }

  .chip.success {
    border-color: rgba(var(--success-color-rgb), 0.3);
    background: rgba(var(--success-color-rgb), 0.08);
  }
  .chip.success .chip-value { color: var(--success-text); }

  .chip.warning {
    border-color: rgba(var(--warning-color-rgb), 0.3);
    background: rgba(var(--warning-color-rgb), 0.08);
  }
  .chip.warning .chip-value { color: var(--warning-text); }

  .chip.info {
    border-color: rgba(var(--accent-rgb), 0.3);
    background: rgba(var(--accent-rgb), 0.08);
  }
  .chip.info .chip-value { color: rgb(var(--accent-rgb)); }

  .chip.danger {
    border-color: rgba(var(--error-color-rgb), 0.3);
    background: rgba(var(--error-color-rgb), 0.08);
  }
  .chip.danger .chip-value { color: var(--error-text); }

</style>
