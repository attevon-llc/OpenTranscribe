<script lang="ts">
  import Toggle from '$components/ui/Toggle.svelte';
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
    <span title={$t('settings.watchSources.fields.enabled')}>
      <Toggle
        checked={source.is_enabled}
        onchange={() => dispatch('toggle', source)}
        disabled={saving}
        label={$t('settings.watchSources.fields.enabled')}
      />
    </span>
    <button class="btn btn-secondary" on:click={() => dispatch('test', source)} disabled={testing}>
      {#if testing}<Spinner size="small" />{:else}{$t('settings.watchSources.test')}{/if}
    </button>
    <button
      class="btn btn-secondary"
      on:click={() => dispatch('scan', source)}
      disabled={!source.is_enabled}
    >
      {$t('settings.watchSources.scanNow')}
    </button>
    <button class="btn btn-secondary" on:click={() => dispatch('files', source)}>
      {$t('settings.watchSources.files.button')}
    </button>
    <button class="btn btn-secondary" on:click={() => dispatch('notifications', source)}>
      {$t('settings.emailNotifications.links.button')}
    </button>
    <button class="btn btn-secondary" on:click={() => dispatch('edit', source)}>
      {$t('common.edit')}
    </button>
    <button class="btn btn-danger" on:click={() => dispatch('delete', source)}>
      {$t('common.delete')}
    </button>
  </div>
</div>

<style>
  .source-card {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-3-5);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background: var(--surface-color);
  }
  .source-badges {
    display: flex;
    gap: var(--space-1-5);
    margin-bottom: var(--space-1);
    flex-wrap: wrap;
  }
  .badge {
    font-size: 0.7rem;
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-xl);
    font-weight: var(--font-weight-semibold);
  }
  .type-badge {
    background: var(--button-hover);
    color: var(--text-secondary);
  }
  .owner-badge {
    background: rgba(var(--primary-color-rgb), 0.15);
    color: var(--primary-on-surface);
  }
  .badge-success {
    background: rgba(34, 197, 94, 0.15);
    color: var(--success-text);
  }
  .badge-error {
    background: rgba(239, 68, 68, 0.15);
    color: var(--error-text);
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
    font-weight: var(--font-weight-semibold);
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
    margin-top: var(--space-1-5);
    font-size: var(--font-size-xs);
    color: var(--warning-text);
  }
  .source-actions {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    flex-wrap: wrap;
  }
</style>
