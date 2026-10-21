<!--
  LocalModelLibrary.svelte - download and activate local Whisper models (issue #1246). Mounted on
  Transcription > Provider by ASRSettings, for administrators only.

  Every route here is super_admin: `locked` (an admin) renders the explanation and makes NO
  request. Progress arrives on the `local-model-download` window event (websocket store); while a
  download runs and the socket has been quiet, the catalog is re-read every few seconds, which also
  covers a reload mid-download. Errors are shown as fixed translated sentences chosen by code.
-->
<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import SettingsCard from '$components/settings/transcription/SettingsCard.svelte';
  import LocalModelRow from './LocalModelRow.svelte';
  import ActivateModelDialog from './ActivateModelDialog.svelte';
  import {
    cancelLocalModelDownload,
    getLocalModelCatalog,
    startLocalModelDownload,
    type LocalModelCatalog,
    type LocalModelDownloadEvent,
    type LocalModelDownloadState,
  } from '$lib/api/localModels';
  import { ASRSettingsApi } from '$lib/api/asrSettings';
  import {
    apiErrorCode,
    apiErrorNumbers,
    errorKeyFor,
    formatBytes,
    isDownloadActive,
  } from '$lib/settings/localModelView';
  import { activateLocalModel } from '$lib/settings/localModelActivation';
  import { toastStore } from '$stores/toast';
  import { locale, t } from '$stores/locale';

  /** An admin who is not a super administrator: read-only, and nothing is requested. */
  export let locked = false;

  const dispatch = createEventDispatcher<{ activated: void }>();
  const POLL_MS = 3000;
  const SOCKET_QUIET_MS = 4000;

  let catalog: LocalModelCatalog | null = null;
  let download: LocalModelDownloadState | null = null;
  let loading = false;
  let loadError = '';
  let notice = '';
  let rowErrors: Record<string, string> = {};

  let confirmFor: string | null = null;
  let switching = false;
  let switchModel = '';
  let switchPhase: 'restarting' | 'running' | 'pending' | null = null;
  let jobsRunning = 0;
  let abort: AbortController | null = null;

  let lastEventAt = 0;
  let pollTimer: ReturnType<typeof setInterval> | undefined;

  $: active = isDownloadActive(download);
  $: if (active && !pollTimer) pollTimer = setInterval(pollTick, POLL_MS);
  $: if (!active && pollTimer) stopPolling();

  function stopPolling() {
    clearInterval(pollTimer);
    pollTimer = undefined;
  }

  function pollTick() {
    if (Date.now() - lastEventAt > SOCKET_QUIET_MS) void load(true);
  }

  function sentenceFor(err: unknown): string {
    const code = apiErrorCode(err);
    const numbers = apiErrorNumbers(err);
    if (!numbers) return $t(errorKeyFor(code));
    return $t(errorKeyFor(code), {
      required: formatBytes(numbers.required, $locale),
      free: formatBytes(numbers.free, $locale),
    });
  }

  function nameOf(id: string): string {
    return catalog?.models.find((m) => m.id === id)?.display_name ?? id;
  }

  /** Adopt a newer download state; a finish while it was running toasts once and re-reads the catalog. */
  function adopt(next: LocalModelDownloadState | null) {
    const wasActive = isDownloadActive(download);
    download = next;
    if (!next || !wasActive || isDownloadActive(next)) return;
    const model = nameOf(next.model_id);
    if (next.status === 'completed') toastStore.success($t('settings.localModels.toast.completed', { model }));
    else if (next.status === 'failed') toastStore.error($t('settings.localModels.toast.failed', { model }));
    else if (next.status === 'cancelled') toastStore.info($t('settings.localModels.toast.cancelled', { model }));
    void load(true);
  }

  async function load(silent = false) {
    if (!silent) loading = true;
    try {
      const next = await getLocalModelCatalog();
      catalog = next;
      loadError = '';
      adopt(next.download);
    } catch (err) {
      if (!silent || !catalog) loadError = sentenceFor(err);
    } finally {
      loading = false;
    }
  }

  function onSocket(event: Event) {
    const data = (event as CustomEvent<LocalModelDownloadEvent>).detail;
    if (!data) return;
    lastEventAt = Date.now();
    adopt({ ...data, updated_at: new Date().toISOString() });
  }

  async function startDownload(modelId: string) {
    rowErrors = { ...rowErrors, [modelId]: '' };
    notice = '';
    try {
      adopt(await startLocalModelDownload(modelId));
    } catch (err) {
      rowErrors = { ...rowErrors, [modelId]: sentenceFor(err) };
      const code = apiErrorCode(err);
      if (code === 'model_download_in_progress' || code === 'local_model_already_downloaded') void load(true);
    }
  }

  async function cancelDownload() {
    notice = '';
    try {
      adopt(await cancelLocalModelDownload());
    } catch (err) {
      notice = sentenceFor(err);
      void load(true);
    }
  }

  async function confirmActivation() {
    const modelId = confirmFor;
    confirmFor = null;
    if (!modelId) return;
    notice = '';
    switching = true;
    switchModel = modelId;
    switchPhase = 'restarting';
    jobsRunning = 0;
    abort = new AbortController();
    try {
      const outcome = await activateLocalModel(modelId, {
        api: {
          setLocalModel: (id) => ASRSettingsApi.setLocalModel(id),
          restartGpuWorker: () => ASRSettingsApi.restartGpuWorker(),
          getLoadedModels: async () => (await ASRSettingsApi.getActiveLocalModel()).loaded_models ?? [],
        },
        onRestarted: (jobs) => (jobsRunning = jobs),
        signal: abort.signal,
      });
      if (outcome === 'aborted') return;
      switchPhase = outcome === 'confirmed' ? 'running' : 'pending';
      await load(true);
      dispatch('activated');
    } catch (err) {
      switchPhase = null;
      notice = sentenceFor(err);
      void load(true);
    } finally {
      switching = false;
    }
  }

  onMount(() => {
    if (locked) return;
    window.addEventListener('local-model-download', onSocket);
    void load();
  });

  onDestroy(() => {
    stopPolling();
    abort?.abort();
    if (typeof window !== 'undefined') window.removeEventListener('local-model-download', onSocket);
  });
</script>

<div class="library tx-form" data-testid="local-model-library">
  <SettingsCard
    title={$t('settings.localModels.title')}
    description={$t('settings.localModels.description')}
    tooltip={$t('settings.localModels.tip')}
  >
    {#if locked}
      <p class="hint locked" data-testid="local-model-locked-hint">{$t('settings.localModels.locked')}</p>
    {:else if loading && !catalog}
      <div class="state"><Spinner size="medium" /></div>
    {:else if loadError && !catalog}
      <p class="error-text" role="alert">{loadError}</p>
      <button type="button" class="btn btn-secondary" on:click={() => load()}>
        {$t('settings.localModels.retry')}
      </button>
    {:else if catalog}
      <p class="hint free-space">
        {$t('settings.localModels.freeSpace', { free: formatBytes(catalog.free_bytes, $locale) })}
      </p>
      {#if !catalog.cache_writable}
        <p class="error-text" role="alert">{$t('settings.localModels.error.cache_not_writable')}</p>
      {/if}
      {#if active}
        <p class="hint">{$t('settings.localModels.oneAtATime')}</p>
      {/if}
      {#if notice}
        <p class="error-text" role="alert">{notice}</p>
      {/if}

      <ul class="rows" aria-label={$t('settings.localModels.listLabel')}>
        {#each catalog.models as entry (entry.id)}
          <LocalModelRow
            {entry}
            {download}
            cacheWritable={catalog.cache_writable}
            locale={$locale}
            {switching}
            errorText={rowErrors[entry.id] ?? ''}
            on:download={(e) => startDownload(e.detail)}
            on:cancel={cancelDownload}
            on:activate={(e) => (confirmFor = e.detail)}
          />
        {/each}
      </ul>

      <div class="switch-status" role="status" aria-live="polite" data-testid="local-model-switch-status">
        {#if switchPhase === 'restarting'}
          <Spinner size="small" />
          <span>{$t('settings.localModels.restarting')}</span>
          {#if jobsRunning > 0}
            <span>{$t('settings.localModels.jobsRunning', { count: jobsRunning })}</span>
          {/if}
        {:else if switchPhase === 'running'}
          <span>{$t('settings.localModels.running', { model: nameOf(switchModel) })}</span>
        {:else if switchPhase === 'pending'}
          <span>{$t('settings.localModels.notReportedYet', { model: nameOf(switchModel) })}</span>
        {/if}
      </div>
    {/if}
  </SettingsCard>
</div>

<ActivateModelDialog
  isOpen={confirmFor !== null}
  model={confirmFor ? nameOf(confirmFor) : ''}
  on:confirm={confirmActivation}
  on:cancel={() => (confirmFor = null)}
/>

<style>
  .library {
    margin-top: 1rem;
  }
  .rows {
    list-style: none;
    margin: 0.5rem 0 0;
    padding: 0;
  }
  .hint {
    margin: 0 0 0.5rem;
    font-size: 0.8rem;
    color: var(--text-secondary);
  }
  .locked {
    padding: 0.75rem 1rem;
    border: 1px dashed var(--border-color);
    border-radius: 8px;
  }
  .error-text {
    margin: 0 0 0.5rem;
    font-size: 0.8rem;
    color: var(--error-color, #b91c1c);
  }
  :global([data-theme='dark']) .error-text {
    color: var(--error-color, #f87171);
  }
  .state {
    display: flex;
    justify-content: center;
    padding: 1rem;
  }
  .switch-status {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.5rem;
    font-size: 0.85rem;
    color: var(--text-color);
  }
  .switch-status:empty {
    display: none;
  }
</style>
