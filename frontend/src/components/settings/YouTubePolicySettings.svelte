<script lang="ts">
  import { onMount } from 'svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import SettingsCard from '$components/settings/transcription/SettingsCard.svelte';
  import {
    getYoutubeSettings,
    updateYoutubeSettings,
    type YoutubeSettings
  } from '$lib/api/youtubeSettings';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';

  let loading = true;
  let saving = false;
  let form: YoutubeSettings | null = null;
  let saved: YoutubeSettings | null = null;

  $: changed = form !== null && saved !== null && JSON.stringify(form) !== JSON.stringify(saved);

  onMount(async () => {
    try {
      saved = await getYoutubeSettings();
      form = { ...saved };
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.youtube.loadFailed')));
    } finally {
      loading = false;
    }
  });

  async function save() {
    if (!form) return;
    saving = true;
    try {
      saved = await updateYoutubeSettings(form);
      form = { ...saved };
      toastStore.success($t('settings.youtube.saved'));
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.youtube.saveFailed')));
    } finally {
      saving = false;
    }
  }
</script>

<SettingsCard title={$t('settings.youtube.title')} description={$t('settings.youtube.desc')}>
  {#if loading}
    <SettingsSkeleton variant="form" count={3} label={$t('common.loading')} />
  {:else if form}
    <div class="youtube-form">
      <label class="check-row">
        <input type="checkbox" id="youtube-rate-limit" bind:checked={form.user_rate_limit_enabled} />
        <span>{$t('settings.youtube.rateLimitEnabled')}</span>
      </label>
      <label class="field">
        <span>{$t('settings.youtube.perHour')}</span>
        <input
          type="number"
          id="youtube-per-hour"
          class="form-input"
          min="1"
          max="100000"
          bind:value={form.user_rate_limit_per_hour}
          disabled={!form.user_rate_limit_enabled}
        />
      </label>
      <label class="field">
        <span>{$t('settings.youtube.perDay')}</span>
        <input
          type="number"
          id="youtube-per-day"
          class="form-input"
          min="1"
          max="100000"
          bind:value={form.user_rate_limit_per_day}
          disabled={!form.user_rate_limit_enabled}
        />
      </label>
      <label class="check-row">
        <input type="checkbox" id="youtube-auto-retry" bind:checked={form.auto_retry_enabled} />
        <span>{$t('settings.youtube.autoRetry')}</span>
      </label>
      <label class="field">
        <span>{$t('settings.youtube.recoveryBatch')}</span>
        <input
          type="number"
          id="youtube-recovery-batch"
          class="form-input"
          min="1"
          max="100"
          bind:value={form.recovery_batch_size}
          disabled={!form.auto_retry_enabled}
        />
        <small>{$t('settings.youtube.recoveryBatchHint')}</small>
      </label>
      <div class="actions">
        <button type="button" class="btn btn-primary" on:click={save} disabled={saving || !changed}>
          {saving ? $t('settings.youtube.saving') : $t('settings.youtube.save')}
        </button>
      </div>
    </div>
  {/if}
</SettingsCard>

<style>
  .youtube-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .check-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-md);
    color: var(--text-color);
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    font-size: var(--font-size-md);
    color: var(--text-color);
    max-width: 22rem;
  }
  .field small {
    color: var(--text-muted);
  }
  .form-input {
    padding: var(--space-1-5) var(--space-2);
    width: 8rem;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
  }
</style>
