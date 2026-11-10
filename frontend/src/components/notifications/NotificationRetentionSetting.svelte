<script lang="ts">
  import { t } from '$stores/locale';
  import { NOTIFICATION_RETENTION_KEYS, labelFor } from '$lib/i18n/keyMaps';
  import { notificationRetention } from '$stores/notificationRetention';
  import { RETENTION_SETTINGS, type RetentionSetting } from '$lib/notifications/retention';

  function onChange(event: Event) {
    notificationRetention.set((event.currentTarget as HTMLSelectElement).value as RetentionSetting);
  }
</script>

<!-- Implicit label: pairs the text with the select without needing an id. -->
<label class="retention-setting">
  <span>{$t('notifications.retention.label')}</span>
  <select value={$notificationRetention} on:change={onChange}>
    {#each RETENTION_SETTINGS as option (option)}
      <option value={option}>{labelFor($t, NOTIFICATION_RETENTION_KEYS, option)}</option>
    {/each}
  </select>
</label>

<style>
  .retention-setting {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-2-5) var(--space-5);
    border-top: 1px solid var(--border-color);
    background: var(--surface-color);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  span {
    flex: 0 1 auto;
  }

  select {
    flex: 0 1 60%;
    min-width: 0;
    padding: var(--space-1) var(--space-2);
    font-size: var(--font-size-sm);
  }

  @media (max-width: 640px) {
    .retention-setting {
      padding: var(--space-2-5) var(--space-4);
    }
  }
</style>
