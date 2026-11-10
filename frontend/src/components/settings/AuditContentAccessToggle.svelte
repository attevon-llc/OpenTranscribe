<!--
  AuditContentAccessToggle.svelte — the super_admin switch for content-access auditing (#1277).

  View, media-access and export events are high volume, so the server records them only while
  this is on (and the edition allows it). Off by default. The server answers what is in effect
  and the storage estimate; this renders it and never decides it.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import Toggle from '$components/ui/Toggle.svelte';
  import { AdminApi, type ContentAccessAuditStatus } from '$lib/api/admin';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';

  let status: ContentAccessAuditStatus | null = null;
  let saving = false;

  $: perUserDayKb = status ? Math.round(status.bytes_per_active_user_per_day_estimate / 1000) : 0;
  // 1,000 active users for 30 days, in GB with one decimal.
  $: perMonthGb = status
    ? ((status.bytes_per_active_user_per_day_estimate * 1000 * 30) / 1e9).toFixed(1)
    : '0';
  $: dedupMinutes = status ? Math.round(status.dedup_seconds / 60) : 0;

  onMount(async () => {
    try {
      status = await AdminApi.getContentAccessAudit();
    } catch (error) {
      console.error('Failed to load the content access auditing setting:', error);
      toastStore.error($t('settings.auditLog.contentAccess.failed'));
    }
  });

  async function change(enabled: boolean) {
    saving = true;
    try {
      status = await AdminApi.setContentAccessAudit(enabled);
      toastStore.success(
        $t(
          status.enabled
            ? 'settings.auditLog.contentAccess.savedOn'
            : 'settings.auditLog.contentAccess.savedOff'
        )
      );
    } catch (error) {
      console.error('Failed to change content access auditing:', error);
      toastStore.error($t('settings.auditLog.contentAccess.failed'));
      // Re-read, so the switch shows what the server holds rather than what was clicked.
      status = await AdminApi.getContentAccessAudit().catch(() => status);
    } finally {
      saving = false;
    }
  }
</script>

{#if status}
  <div class="content-access" data-testid="content-access-audit">
    <div class="copy">
      <label for="content-access-audit-toggle" class="title">
        {$t('settings.auditLog.contentAccess.title')}
      </label>
      <p class="help">{$t('settings.auditLog.contentAccess.help')}</p>
      <p class="help">
        {$t('settings.auditLog.contentAccess.storage', {
          perUserDay: perUserDayKb,
          perMonth: perMonthGb,
          minutes: dedupMinutes,
        })}
      </p>
      {#if !status.capability_available}
        <p class="note" data-testid="content-access-unavailable">
          {$t('settings.auditLog.contentAccess.unavailable')}
        </p>
      {/if}
    </div>
    <Toggle
      id="content-access-audit-toggle"
      checked={status.enabled}
      busy={saving}
      onchange={change}
    />
  </div>
{/if}

<style>
  .content-access {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-4);
    padding: var(--space-3);
    margin-bottom: var(--space-3);
    background: var(--color-bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
  }

  .copy {
    min-width: 0;
  }

  .title {
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .help {
    margin: var(--space-1) 0 0;
    font-size: var(--font-size-xs);
    line-height: 1.4;
    color: var(--text-secondary);
  }

  .note {
    margin: 0.4rem 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-color);
  }
</style>
