<script lang="ts">
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

<div class="section-head admin-section">
  <h4>{$t('settings.watchSources.globalHeading')}</h4>
</div>
<div class="global-settings">
  <label class="checkbox-row">
    <input type="checkbox" bind:checked={settings.enabled} />
    <span>{$t('settings.watchSources.global.enabled')}</span>
  </label>
  <div class="form-row">
    <div class="form-group">
      <label for="gs-stab">{$t('settings.watchSources.global.fileStability')}</label>
      <input
        id="gs-stab"
        type="number"
        min="0"
        class="form-input"
        bind:value={settings.file_stability_seconds}
      />
    </div>
    <div class="form-group">
      <label for="gs-max-per-scan">{$t('settings.watchSources.global.maxImportsPerScan')}</label>
      <input
        id="gs-max-per-scan"
        type="number"
        min="1"
        class="form-input"
        bind:value={settings.max_imports_per_scan}
      />
      <small class="form-hint">{$t('settings.watchSources.global.maxImportsPerScanHelp')}</small>
    </div>
  </div>
  <label class="checkbox-row">
    <input type="checkbox" bind:checked={settings.fs_events_enabled} />
    <span>{$t('settings.watchSources.global.fsEvents')}</span>
  </label>
  {#if settings.fs_events_enabled}
    <div class="form-row">
      <div class="form-group">
        <label for="gs-fs-mode">{$t('settings.watchSources.global.fsEventsMode')}</label>
        <select id="gs-fs-mode" class="form-input" bind:value={settings.fs_events_mode}>
          <option value="auto">{$t('settings.watchSources.global.fsEventsModeAuto')}</option>
          <option value="native">{$t('settings.watchSources.global.fsEventsModeNative')}</option>
          <option value="polling">{$t('settings.watchSources.global.fsEventsModePolling')}</option>
          <option value="off">{$t('settings.watchSources.global.fsEventsModeOff')}</option>
        </select>
        <small class="form-hint">{$t('settings.watchSources.global.fsEventsModeHelp')}</small>
      </div>
      <div class="form-group">
        <label for="gs-fs-poll">{$t('settings.watchSources.global.fsEventsPollSeconds')}</label>
        <input
          id="gs-fs-poll"
          type="number"
          min="1"
          class="form-input"
          bind:value={settings.fs_events_poll_seconds}
        />
        <small class="form-hint">{$t('settings.watchSources.global.fsEventsPollSecondsHelp')}</small>
      </div>
    </div>
  {/if}
  <button class="btn btn-primary" on:click={() => dispatch('save')} disabled={saving}>
    {saving ? $t('common.saving') : $t('common.save')}
  </button>
</div>

<style>
  .section-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: 8px 0 12px;
  }
  .section-head h4 {
    margin: 0;
  }
  .admin-section {
    margin-top: 28px;
    border-top: 1px solid var(--border-color);
    padding-top: 16px;
  }
  .global-settings,
  .form-group {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .form-row {
    display: flex;
    gap: 12px;
  }
  .form-group {
    flex: 1;
    gap: 4px;
  }
  .form-group label {
    font-size: 0.85rem;
    color: var(--text-secondary);
  }
  .form-hint {
    display: block;
    margin-top: 0.35rem;
    font-size: 0.8rem;
    color: var(--text-secondary);
    font-style: italic;
  }
  /* .form-input inherits the global input styling (form-elements.css). */
  .checkbox-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.9rem;
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
