<!--
  AuditContentAccessToggle.svelte — the super_admin switch for content-access auditing (#1277).

  View, media-access and export events are high volume, so the server records them only while
  this is on (and the edition allows it). Off by default. The server answers what is in effect
  and the storage estimate; this renders it and never decides it.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { AdminApi, type ContentAccessAuditStatus } from '$lib/api/admin';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';

  let status: ContentAccessAuditStatus | null = null;
  let saving = false;
  let toggle: HTMLInputElement;

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

  async function change(event: Event) {
    const enabled = (event.target as HTMLInputElement).checked;
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
      // A click flips the box before the server answers; when the answer is "no change" the
      // `checked` attribute binding sees the same value and leaves the box as clicked.
      if (toggle && status) toggle.checked = status.enabled;
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
    <label class="toggle-switch">
      <input
        bind:this={toggle}
        id="content-access-audit-toggle"
        type="checkbox"
        checked={status.enabled}
        disabled={saving}
        on:change={change}
      />
      <span class="toggle-slider"></span>
    </label>
  </div>
{/if}

<style>
  .content-access {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem;
    margin-bottom: 0.75rem;
    background: var(--color-bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
  }

  .copy {
    min-width: 0;
  }

  .title {
    font-weight: 600;
    color: var(--text-color);
  }

  .help {
    margin: 0.25rem 0 0;
    font-size: 0.75rem;
    line-height: 1.4;
    color: var(--text-secondary);
  }

  .note {
    margin: 0.4rem 0 0;
    font-size: 0.75rem;
    color: var(--text-color);
  }

  .toggle-switch {
    position: relative;
    display: inline-block;
    width: 44px;
    height: 24px;
    flex-shrink: 0;
  }

  .toggle-switch input {
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background-color: var(--border-color);
    transition: 0.3s;
    border-radius: 24px;
  }

  .toggle-slider:before {
    position: absolute;
    content: '';
    height: 18px;
    width: 18px;
    inset-inline-start: 3px;
    bottom: 3px;
    background-color: var(--toggle-button, white);
    transition: 0.3s;
    border-radius: 50%;
  }

  .toggle-switch input:focus-visible + .toggle-slider {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .toggle-switch input:checked + .toggle-slider {
    background-color: var(--primary-color);
  }

  .toggle-switch input:disabled + .toggle-slider {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .toggle-switch input:checked + .toggle-slider:before {
    transform: translateX(20px);
  }

  :global([dir='rtl']) .toggle-switch input:checked + .toggle-slider:before {
    transform: translateX(-20px);
  }
</style>
