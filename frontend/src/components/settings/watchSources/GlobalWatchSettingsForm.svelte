<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  /**
   * Deployment-wide watch settings (super_admin tier).
   *
   * `settings` is bound so the coordinator holds the single source of truth and can
   * PUT it; this component only renders the form and asks for a save.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { GlobalWatchSettings } from '$lib/api/watchSourcesApi';

  export let settings: GlobalWatchSettings;
  export let saving = false;

  const dispatch = createEventDispatcher<{ save: void }>();
</script>

<div class="section-head">
  <h4>{$t('settings.watchSources.globalHeading')}</h4>
</div>
<div class="global-settings">
  <label class="checkbox-row">
    <input type="checkbox" bind:checked={settings.enabled} />
    <span>{$t('settings.watchSources.global.enabled')}</span>
  </label>
  <div class="form-row">
    <FormField id="gs-stab" label={$t('settings.watchSources.global.fileStability')} labelSize="md">
      <input
        id="gs-stab"
        type="number"
        min="0"
        class="form-input"
        bind:value={settings.file_stability_seconds}
      />
    </FormField>
    <FormField id="gs-max-per-scan" label={$t('settings.watchSources.global.maxImportsPerScan')} labelSize="md" hint={$t('settings.watchSources.global.maxImportsPerScanHelp')} let:describedby>
      <input
        id="gs-max-per-scan" aria-describedby={describedby}
        type="number"
        min="1"
        class="form-input"
        bind:value={settings.max_imports_per_scan}
      />
    </FormField>
  </div>
  <label class="checkbox-row">
    <input type="checkbox" bind:checked={settings.fs_events_enabled} />
    <span>{$t('settings.watchSources.global.fsEvents')}</span>
  </label>
  {#if settings.fs_events_enabled}
    <div class="form-row">
      <FormField id="gs-fs-mode" label={$t('settings.watchSources.global.fsEventsMode')} labelSize="md" hint={$t('settings.watchSources.global.fsEventsModeHelp')} let:describedby>
        <select id="gs-fs-mode" aria-describedby={describedby} class="form-input" bind:value={settings.fs_events_mode}>
          <option value="auto">{$t('settings.watchSources.global.fsEventsModeAuto')}</option>
          <option value="native">{$t('settings.watchSources.global.fsEventsModeNative')}</option>
          <option value="polling">{$t('settings.watchSources.global.fsEventsModePolling')}</option>
          <option value="off">{$t('settings.watchSources.global.fsEventsModeOff')}</option>
        </select>
      </FormField>
      <FormField id="gs-fs-poll" label={$t('settings.watchSources.global.fsEventsPollSeconds')} labelSize="md" hint={$t('settings.watchSources.global.fsEventsPollSecondsHelp')} let:describedby>
        <input
          id="gs-fs-poll" aria-describedby={describedby}
          type="number"
          min="1"
          class="form-input"
          bind:value={settings.fs_events_poll_seconds}
        />
      </FormField>
    </div>
  {/if}
  <div class="form-actions">
    <button class="btn btn-primary" on:click={() => dispatch('save')} disabled={saving}>
      {saving ? $t('common.saving') : $t('common.save')}
    </button>
  </div>
</div>

<style>
  .section-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: var(--space-2) 0 var(--space-3);
  }
  .section-head h4 {
    margin: 0;
  }
  .form-actions {
    display: flex;
    justify-content: flex-end;
    padding-top: var(--space-2);
    border-top: 1px solid var(--border-color);
  }
  .global-settings,
  .form-row > :global(.form-field) {
    flex: 1;
  }
  .form-row {
    display: flex;
    gap: var(--space-3);
  }
  /* .form-input inherits the global input styling (form-elements.css). */
  .checkbox-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-body);
    cursor: pointer;
  }
  /* Override the global `input { width:100% }` base so checkboxes stay square. */
  .checkbox-row input[type='checkbox'] {
    width: 16px;
    height: 16px;
    min-height: 0;
    margin: 0;
    padding: 0;
    flex: none;
    cursor: pointer;
    accent-color: var(--primary-color);
  }
</style>
