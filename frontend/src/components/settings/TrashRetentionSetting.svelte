<!--
  TrashRetentionSetting.svelte: the trash window (issue #1101), in Settings, Data & Storage,
  File Retention. Admin only, like the panel around it.

  Its own block and its own Save because it is a different concern from retention expiry: it
  applies whether or not retention is enabled, and retention expiry never uses the trash. It
  shares the retention config read (`sectionData`) and sends only `trash_retention_days`, which
  the PUT treats as a partial update. The server's 1-90 bound is the rule; the field checks it
  first only to explain a refusal before the round trip.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import axiosInstance from '$lib/axios';
  import {
    fetchRetentionConfig,
    loadSectionData,
    peekSectionData,
    SectionDataKey,
    setSectionData,
  } from '$lib/settings/sectionData';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';

  interface TrashWindow {
    trash_retention_days: number;
  }

  const MIN_DAYS = 1;
  const MAX_DAYS = 90;

  let days: number | null = null;
  let saved: number | null = null;
  let saving = false;

  $: valid = days !== null && Number.isInteger(days) && days >= MIN_DAYS && days <= MAX_DAYS;
  $: dirty = days !== saved;

  function adopt(cfg: TrashWindow | undefined) {
    if (!cfg || typeof cfg.trash_retention_days !== 'number') return;
    // Never overwrite an edit in progress with a background refresh.
    if (days === saved) days = cfg.trash_retention_days;
    saved = cfg.trash_retention_days;
  }

  onMount(async () => {
    adopt(peekSectionData<TrashWindow>(SectionDataKey.retention));
    try {
      adopt(await loadSectionData(SectionDataKey.retention, () => fetchRetentionConfig<TrashWindow>()));
    } catch (err) {
      console.error('Error loading the trash window:', err);
    }
  });

  async function save() {
    if (!valid || days === null) return;
    saving = true;
    try {
      const res = await axiosInstance.put('/admin/settings/retention-config', {
        trash_retention_days: days,
      });
      setSectionData(SectionDataKey.retention, res.data);
      saved = res.data.trash_retention_days;
      days = saved;
      toastStore.success($t('settings.retention.trashSaved'));
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.retention.trashSaveFailed')));
    } finally {
      saving = false;
    }
  }
</script>

<section class="trash-window" data-testid="trash-retention-setting">
  <h4 class="trash-title">{$t('settings.retention.trashTitle')}</h4>
  <div class="field-group">
    <label class="field-label" for="trash-retention-days">
      {$t('settings.retention.trashDaysLabel')}
    </label>
    <div class="inline-input">
      <input
        id="trash-retention-days"
        type="number"
        class="form-input number-input"
        min={MIN_DAYS}
        max={MAX_DAYS}
        step="1"
        bind:value={days}
        disabled={saved === null || saving}
        aria-invalid={days !== null && !valid}
        aria-describedby="trash-retention-help"
      />
      <span class="input-suffix">{$t('settings.retention.daysUnit')}</span>
      <button
        type="button"
        class="btn btn-primary btn-sm"
        disabled={!dirty || !valid || saving}
        on:click={save}
      >
        {saving ? $t('settings.retention.savingButton') : $t('settings.retention.saveButton')}
      </button>
    </div>
    {#if days !== null && !valid}
      <p class="field-error" role="alert">{$t('settings.retention.trashDaysInvalid')}</p>
    {/if}
    <p class="field-help" id="trash-retention-help">{$t('settings.retention.trashDaysHelp')}</p>
  </div>
</section>

<style>
  .trash-window {
    margin-block-start: var(--space-6);
    padding-block-start: var(--space-4);
    border-block-start: 1px solid var(--border-color);
  }

  .trash-title {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin: 0 0 var(--space-2);
  }

  .field-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
  }

  .field-label {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  .inline-input {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  .number-input {
    inline-size: 6rem;
  }

  .input-suffix {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .field-help {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    margin: 0;
    max-inline-size: var(--measure-prose);
  }

  .field-error {
    font-size: var(--font-size-sm);
    color: var(--error-color);
    margin: 0;
  }
</style>
