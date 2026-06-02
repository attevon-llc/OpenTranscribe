<script lang="ts">
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

  function getPercentage(value: number, total: number): string {
    if (total === 0) return '0';
    return ((value / total) * 100).toFixed(0);
  }
</script>

<div class="account-status-strip">
  {#if loading}
    <div class="skeleton-chips">
      <div class="skeleton-chip"></div>
      <div class="skeleton-chip"></div>
      <div class="skeleton-chip"></div>
      <div class="skeleton-chip wide"></div>
      <div class="skeleton-chip"></div>
    </div>
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
    margin-bottom: 1.25rem;
    padding: 0.75rem 1rem;
    background: var(--background-color, #2a2a2a);
    border: 1px solid var(--border-color, #444);
    border-radius: 10px;
  }

  .status-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
    justify-content: center;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.375rem 0.75rem;
    border-radius: 20px;
    font-size: 0.8rem;
    background: var(--surface-color, #333);
    border: 1px solid var(--border-color, #444);
    color: var(--text-color, #e0e0e0);
  }

  .chip-value {
    font-weight: 700;
    font-size: 0.85rem;
  }

  .chip-label {
    color: var(--text-secondary, #999);
    font-size: 0.75rem;
  }

  .chip.success {
    border-color: rgba(34, 197, 94, 0.3);
    background: rgba(34, 197, 94, 0.08);
  }
  .chip.success .chip-value { color: rgb(34, 197, 94); }

  .chip.warning {
    border-color: rgba(234, 179, 8, 0.3);
    background: rgba(234, 179, 8, 0.08);
  }
  .chip.warning .chip-value { color: rgb(234, 179, 8); }

  .chip.info {
    border-color: rgba(59, 130, 246, 0.3);
    background: rgba(59, 130, 246, 0.08);
  }
  .chip.info .chip-value { color: rgb(59, 130, 246); }

  .chip.danger {
    border-color: rgba(239, 68, 68, 0.3);
    background: rgba(239, 68, 68, 0.08);
  }
  .chip.danger .chip-value { color: rgb(239, 68, 68); }

  /* Skeleton loading */
  .skeleton-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    justify-content: center;
  }

  .skeleton-chip {
    height: 30px;
    width: 100px;
    border-radius: 20px;
    background: var(--border-color, #444);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-chip.wide {
    width: 130px;
  }
</style>
