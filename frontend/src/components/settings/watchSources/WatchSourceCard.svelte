<script lang="ts">
  /**
   * One watch source in the settings list: badges, stats line, action row.
   *
   * Presentational — it owns no data and makes no API call. Every action leaves as
   * an event and the coordinator (`WatchSourcesSettings.svelte`) decides what it
   * means. See `../CLAUDE.md` in this folder.
   */
  import { createEventDispatcher } from 'svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { t } from '$stores/locale';
  import {
    getSourceTypeLabel,
    type WatchSource,
    type Capabilities,
    type WatchSourceStats,
  } from '$lib/api/watchSourcesApi';

  export let source: WatchSource;
  export let stats: WatchSourceStats | undefined = undefined;
  export let capabilities: Capabilities;
  export let saving = false;
  export let testing = false;

  const dispatch = createEventDispatcher<{
    toggle: WatchSource;
    test: WatchSource;
    scan: WatchSource;
    edit: WatchSource;
    delete: WatchSource;
    files: WatchSource;
    notifications: WatchSource;
  }>();

  function scanBadgeClass(status?: string | null): string {
    if (status === 'success') return 'badge-success';
    if (status === 'error') return 'badge-error';
    if (status === 'running') return 'badge-running';
    return 'badge-idle';
  }

  /**
   * Does this source opt into event-driven watching at all? Only local sources
   * can — S3/SMB have no equivalent, so they never show a watch-mode badge.
   */
  function showsWatchMode(s: WatchSource): boolean {
    return s.source_type === 'local' && s.use_fs_events && capabilities.fs_events_enabled;
  }

  /** Short badge label for the observer a source actually ended up with. */
  function watchModeLabel(s: WatchSource): string {
    const mode = s.fs_events?.mode;
    if (mode === 'native') return $t('settings.watchSources.watchMode.native');
    if (mode === 'polling') return $t('settings.watchSources.watchMode.polling');
    if (mode === 'error') return $t('settings.watchSources.watchMode.error');
    if (mode === 'unavailable') return $t('settings.watchSources.watchMode.unavailable');
    return $t('settings.watchSources.watchMode.scanOnly', {
      minutes: s.polling_interval_minutes,
    });
  }

  function watchModeClass(s: WatchSource): string {
    const mode = s.fs_events?.mode;
    if (mode === 'native') return 'badge-success';
    if (mode === 'polling') return 'badge-running';
    if (mode === 'error' || mode === 'unavailable') return 'badge-error';
    return 'badge-idle';
  }

  /** Tooltip: the backend's own explanation, or why nothing is watching. */
  function watchModeTitle(s: WatchSource): string {
    if (s.fs_events?.detail) return s.fs_events.detail;
    return $t('settings.watchSources.watchMode.scanOnlyHelp', {
      minutes: s.polling_interval_minutes,
    });
  }
</script>

<!-- `.source-card` is E2E-guarded (backend/tests/e2e/test_watch_sources_e2e.py) -->
<div class="source-card">
  <div class="source-main">
    <div class="source-badges">
      <span class="badge type-badge">{getSourceTypeLabel(source.source_type)}</span>
      <span class="badge {scanBadgeClass(source.last_scan_status)}">
        {source.last_scan_status || $t('settings.watchSources.neverScanned')}
      </span>
      {#if showsWatchMode(source)}
        <span class="badge {watchModeClass(source)}" title={watchModeTitle(source)}>
          {watchModeLabel(source)}
        </span>
      {/if}
      {#if !source.is_own}
        <span class="badge owner-badge">{source.owner_name}</span>
      {/if}
    </div>
    <div class="source-name">{source.name}</div>
    <div class="source-meta">
      {#if stats}
        {$t('settings.watchSources.statsLine', {
          imported: stats.imported,
          skipped: stats.skipped,
          error: stats.error,
        })}
      {/if}
      {#if source.last_scan_message}<span class="scan-msg"> · {source.last_scan_message}</span>{/if}
    </div>
    {#if source.source_type === 'local' && !source.delete_after_import}
      <div class="disk-banner">{$t('settings.watchSources.diskBanner')}</div>
    {/if}
  </div>
  <div class="source-actions">
    <label class="enable-toggle" title={$t('settings.watchSources.fields.enabled')}>
      <input
        type="checkbox"
        checked={source.is_enabled}
        on:change={() => dispatch('toggle', source)}
        disabled={saving}
      />
      <span class="slider"></span>
    </label>
    <button class="btn btn-secondary btn-sm" on:click={() => dispatch('test', source)} disabled={testing}>
      {#if testing}<Spinner size="small" />{:else}{$t('settings.watchSources.test')}{/if}
    </button>
    <button
      class="btn btn-secondary btn-sm"
      on:click={() => dispatch('scan', source)}
      disabled={!source.is_enabled}
    >
      {$t('settings.watchSources.scanNow')}
    </button>
    <button class="btn btn-secondary btn-sm" on:click={() => dispatch('files', source)}>
      {$t('settings.watchSources.files.button')}
    </button>
    <button class="btn btn-secondary btn-sm" on:click={() => dispatch('notifications', source)}>
      {$t('settings.emailNotifications.links.button')}
    </button>
    <button class="btn btn-secondary btn-sm" on:click={() => dispatch('edit', source)}>
      {$t('common.edit')}
    </button>
    <button class="btn btn-danger btn-sm" on:click={() => dispatch('delete', source)}>
      {$t('common.delete')}
    </button>
  </div>
</div>

<style>
  .source-card {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
    padding: 12px 14px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
  }
  .source-badges {
    display: flex;
    gap: 6px;
    margin-bottom: 4px;
    flex-wrap: wrap;
  }
  .badge {
    font-size: 0.7rem;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
  }
  .type-badge {
    background: var(--button-hover);
    color: var(--text-secondary);
  }
  .owner-badge {
    background: rgba(99, 102, 241, 0.15);
    color: var(--primary-color);
  }
  .badge-success {
    background: rgba(34, 197, 94, 0.15);
    color: var(--success-color, #16a34a);
  }
  .badge-error {
    background: rgba(239, 68, 68, 0.15);
    color: var(--error-color, #dc2626);
  }
  .badge-running {
    background: rgba(234, 179, 8, 0.15);
    color: #ca8a04;
  }
  .badge-idle {
    background: var(--button-hover);
    color: var(--text-secondary);
  }
  .source-name {
    font-weight: 600;
    font-size: 0.95rem;
  }
  .source-meta {
    font-size: 0.8rem;
    color: var(--text-secondary);
  }
  .scan-msg {
    font-style: italic;
  }
  .disk-banner {
    margin-top: 6px;
    font-size: 0.75rem;
    color: var(--warning-color);
  }
  .source-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .btn-sm {
    padding: 4px 10px;
    font-size: 0.8rem;
  }
  .enable-toggle {
    position: relative;
    display: inline-block;
    width: 36px;
    height: 20px;
  }
  .enable-toggle input {
    opacity: 0;
    width: 0;
    height: 0;
  }
  .slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background: var(--border-color);
    border-radius: 20px;
    transition: 0.2s;
  }
  .slider::before {
    content: '';
    position: absolute;
    height: 14px;
    width: 14px;
    left: 3px;
    bottom: 3px;
    background: white;
    border-radius: 50%;
    transition: 0.2s;
  }
  .enable-toggle input:checked + .slider {
    background: var(--primary-color);
  }
  .enable-toggle input:checked + .slider::before {
    transform: translateX(16px);
  }
</style>
