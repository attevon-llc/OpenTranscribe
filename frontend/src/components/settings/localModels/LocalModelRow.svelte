<!--
  LocalModelRow.svelte - one catalog model in the library (issue #1246): what it is, what state it
  is in, and the buttons that apply. Presentational: the library owns every request.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import Badge from '$components/ui/Badge.svelte';
  import ProgressBar from '$components/ui/ProgressBar.svelte';
  import { t } from '$stores/locale';
  import {
    formatBytes,
    LANGUAGE_KEYS,
    rowView,
    STATUS_KEYS,
    type RowStatus,
  } from '$lib/settings/localModelView';
  import type { LocalModelDownloadState, LocalModelEntry } from '$lib/api/localModels';

  export let entry: LocalModelEntry;
  export let download: LocalModelDownloadState | null = null;
  export let cacheWritable = true;
  export let locale = 'en';
  /** An activation is under way: switching is refused until it ends. */
  export let switching = false;
  /** Translated sentence for an error that belongs to this row (e.g. a refused start). */
  export let errorText = '';

  const dispatch = createEventDispatcher<{ download: string; cancel: void; activate: string }>();

  $: view = rowView(entry, download, { cacheWritable });
  $: variant = (
    {
      active: 'success',
      downloaded: 'info',
      partial: 'warning',
      notDownloaded: 'default',
      downloading: 'info',
      queued: 'info',
      verifying: 'info',
    } as Record<RowStatus, 'success' | 'info' | 'warning' | 'default'>
  )[view.status];
  $: statusText =
    view.status === 'downloading' && view.percent !== null
      ? $t('settings.localModels.status.downloadingPercent', { percent: Math.round(view.percent) })
      : $t(STATUS_KEYS[view.status]);
  // Screen readers hear progress in 10% steps, not every tick.
  $: announced =
    view.status === 'downloading' && view.percent !== null
      ? $t('settings.localModels.status.downloadingPercent', {
          percent: Math.floor(view.percent / 10) * 10,
        })
      : statusText;
  $: shownError = errorText || (view.errorKey ? $t(view.errorKey) : '');
</script>

<li class="model-row" class:active={view.status === 'active'} data-testid="local-model-{entry.id}">
  <div class="row-main">
    <div class="identity">
      <span class="model-name">{entry.display_name}</span>
      <Badge variant={variant}>{statusText}</Badge>
      <span class="sr-only" role="status" aria-live="polite">{announced}</span>
    </div>
    <div class="facts">
      <span>{$t('settings.localModels.size', { size: formatBytes(entry.approx_bytes, locale) })}</span>
      <span>{$t('settings.localModels.vram', { gb: entry.vram_hint_gb })}</span>
      <span>{$t(LANGUAGE_KEYS[entry.language_support])}</span>
      <span>
        {entry.supports_translation
          ? $t('settings.localModels.translates')
          : $t('settings.localModels.noTranslation')}
      </span>
    </div>
  </div>

  <div class="actions">
    {#if view.showCancel}
      <button type="button" class="btn btn-secondary" on:click={() => dispatch('cancel')}>
        {$t('settings.localModels.cancel')}
      </button>
    {/if}
    {#if view.showDownload}
      <button
        type="button"
        class="btn btn-primary"
        disabled={view.downloadDisabled}
        aria-label="{entry.partial ? $t('settings.localModels.resume') : $t('settings.localModels.download')} - {entry.display_name}"
        on:click={() => dispatch('download', entry.id)}
      >
        {entry.partial ? $t('settings.localModels.resume') : $t('settings.localModels.download')}
      </button>
    {/if}
    {#if view.showActivate}
      <button
        type="button"
        class="btn btn-secondary"
        disabled={view.activateDisabled || switching}
        aria-label="{$t('settings.localModels.activate')} - {entry.display_name}"
        on:click={() => dispatch('activate', entry.id)}
      >
        {$t('settings.localModels.activate')}
      </button>
    {/if}
  </div>

  {#if view.inFlight}
    <div
      class="progress"
      role="progressbar"
      aria-label={$t('settings.localModels.progressAria', { model: entry.display_name })}
      aria-valuemin="0"
      aria-valuemax="100"
      aria-valuenow={view.percent === null ? undefined : Math.round(view.percent)}
    >
      <ProgressBar percent={view.percent} />
      {#if download && download.bytes_total}
        <span class="bytes">
          {$t('settings.localModels.ofBytes', {
            done: formatBytes(download.bytes_done, locale),
            total: formatBytes(download.bytes_total, locale),
          })}
        </span>
      {/if}
    </div>
  {/if}

  {#if shownError}
    <p class="row-error" role="alert">{shownError}</p>
  {/if}
</li>

<style>
  .model-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 0.5rem 1rem;
    align-items: center;
    padding: 0.85rem 0;
    border-top: 1px solid var(--border-color);
  }
  .model-row:first-child {
    border-top: none;
  }
  .row-main {
    min-width: 0;
  }
  .identity {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
  }
  .model-name {
    font-weight: var(--font-weight-semibold, 600);
    color: var(--text-color);
    overflow-wrap: anywhere;
  }
  .facts {
    display: flex;
    flex-wrap: wrap;
    gap: 0.15rem 0.9rem;
    margin-top: 0.25rem;
    font-size: 0.8rem;
    color: var(--text-secondary);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    justify-content: flex-end;
  }
  .progress,
  .row-error {
    grid-column: 1 / -1;
    margin: 0;
  }
  .bytes {
    display: block;
    margin-top: 0.25rem;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }
  .row-error {
    font-size: 0.8rem;
    color: var(--error-color, #b91c1c);
  }
  :global([data-theme='dark']) .row-error {
    color: var(--error-color, #f87171);
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  @media (max-width: 640px) {
    .model-row {
      grid-template-columns: 1fr;
    }
    .actions {
      justify-content: flex-start;
    }
  }
</style>
