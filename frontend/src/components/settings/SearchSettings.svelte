<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { formatDateTime } from '$lib/utils/formatting';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { onMount, onDestroy } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { t, locale } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { createPoller } from '$lib/utils/poller';
  import { toastStore } from '$stores/toast';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import StatusChip from './StatusChip.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import ProgressBar from '$components/ui/ProgressBar.svelte';
  import LiveProgressCard from '$components/ui/LiveProgressCard.svelte';

  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmLabel,
    variant: confirmVariant,
  } = confirmDialog;

  interface EmbeddingModel {
    model_id: string;
    name: string;
    dimension: number;
    description: string;
    size_mb: number;
    // The ops endpoint has always returned these; the settings UI — where the model is
    // actually chosen — did not, so a multilingual model was identifiable only by
    // reading its display name.
    languages: string[];
    language_type: string;
    // Whether OpenSearch can currently embed with this model. Without it the picker
    // offered every model identically and choosing an undownloaded one answered 409
    // with instructions to call two API endpoints by hand.
    ready: boolean;
  }

  interface IndexStatus {
    total_files: number;
    indexed_files: number;
    pending_files: number;
    in_progress: boolean;
    stop_requested?: boolean;
    // True when the backend could not read the run state; in_progress is then unknown, not false.
    status_unavailable?: boolean;
    current_model: string;
    current_dimension: number;
    last_indexed_at: string | null;
  }

  interface ReindexStats {
    indexed_files: number;
    total_files: number;
    total_chunks: number;
    failed_files: number;
    mode?: string;
  }

  interface ReindexProgress {
    progress: number;
    indexed_files: number;
    total_files: number;
    eta_seconds?: number | null;
    message?: string;
  }

  interface IndexHealthEntry {
    status: 'green' | 'red';
    doc_count: number;
    error: string | null;
  }

  interface BootstrapStatus {
    state: 'ok' | 'degraded' | 'disabled';
    attempts: number;
    last_error: string | null;
    retry_at: string | null;
    text_only_chunk_files: number | null;
  }

  interface DegradedPreview {
    total_files: number;
    truncated: boolean;
    affected_users: number;
    files: Array<{ file_uuid: string; title: string; user_id: number }>;
  }

  let models: EmbeddingModel[] = [];
  let selectedModelId = '';
  let currentModelId = '';
  let indexStatus: IndexStatus | null = null;
  let isLoading = true;
  let isReindexing = false;
  let isSwitchingModel = false;
  let isPreparingModel = false;
  let isStopping = false;
  let indexHealth: Record<string, IndexHealthEntry> | null = null;
  let bootstrapStatus: BootstrapStatus | null = null;
  const bootstrapPoller = createPoller(() => loadBootstrapStatus(), { intervalMs: 30000 });
  const statusPoller = createPoller(() => loadStatus(), { intervalMs: 10000 });

  // Operator-triggered re-embed of neural-search degraded (text-only) files (issue #626,
  // the follow-up to #625's bootstrap self-heal). Preview + confirm, never dispatch-on-click.
  let degradedPreview: DegradedPreview | null = null;
  let isCheckingDegraded = false;
  let isReembedding = false;

  // Live reindex progress
  let reindexProgress: ReindexProgress | null = null;

  // Event handlers for WebSocket events
  function handleReindexProgress(event: CustomEvent<ReindexProgress>) {
    reindexProgress = event.detail;
    isReindexing = true;
  }

  let lastReindexStats: ReindexStats | null = null;

  function handleReindexComplete(event: CustomEvent<{ stats?: ReindexStats }>) {
    reindexProgress = null;
    isReindexing = false;
    lastReindexStats = event.detail?.stats || null;
    // Reload status to get updated counts
    loadStatus();
    // A reindex (including one dispatched by the degraded re-embed) drives the degraded
    // count back down — reload it rather than assuming it hit zero, since a re-embed run
    // can be truncated and leave a remainder.
    loadDegradedPreview();
    toastStore.success($t('search.reindexComplete'));
  }

  function handleReindexStopped() {
    reindexProgress = null;
    isReindexing = false;
    isStopping = false;
    loadStatus();
    toastStore.success($t('search.reindexStopped'));
  }

  onMount(async () => {
    await Promise.all([
      loadModels(),
      loadStatus(),
      loadIndexHealth(),
      loadBootstrapStatus(),
      loadDegradedPreview(),
    ]);
    isLoading = false;

    // Listen for WebSocket events
    window.addEventListener('reindex-progress', handleReindexProgress as EventListener);
    window.addEventListener('reindex-complete', handleReindexComplete as EventListener);
    window.addEventListener('reindex-stopped', handleReindexStopped as EventListener);
  });

  let destroyed = false;

  onDestroy(() => {
    destroyed = true;
    window.removeEventListener('reindex-progress', handleReindexProgress as EventListener);
    window.removeEventListener('reindex-complete', handleReindexComplete as EventListener);
    window.removeEventListener('reindex-stopped', handleReindexStopped as EventListener);
    bootstrapPoller.stop();
    statusPoller.stop();
  });

  // Neural search bootstrap self-heal (issue #625). The backend beat task retries every
  // 10 minutes; polling every 30s here just keeps the banner from going stale while an
  // admin has the page open watching it recover — it does not drive the recovery itself.
  async function loadBootstrapStatus() {
    try {
      const res = await axiosInstance.get('/search/models/neural/status');
      bootstrapStatus = res.data.bootstrap ?? null;
    } catch (e) {
      console.error('Failed to load neural bootstrap status:', e);
      bootstrapStatus = null;
    }
    if (destroyed) return;
    if (bootstrapStatus?.state === 'degraded') {
      bootstrapPoller.start();
    } else {
      bootstrapPoller.stop();
    }
  }

  // Operator-triggered re-embed of neural-search degraded (text-only) files (#626).
  // Preview-only — never dispatches anything. Called on load and again once a reindex
  // (including one this button dispatched) completes, so the count reflects reality
  // rather than being assumed to hit zero.
  async function loadDegradedPreview() {
    isCheckingDegraded = true;
    try {
      const res = await axiosInstance.get('/search/degraded-embeddings');
      degradedPreview = res.data;
    } catch (e) {
      console.error('Failed to load degraded-embeddings preview:', e);
      degradedPreview = null;
    } finally {
      isCheckingDegraded = false;
    }
  }

  function openReembedModal() {
    if (!degradedPreview || degradedPreview.total_files === 0) return;
    const preview = degradedPreview;
    const truncatedNote = preview.truncated
      ? ' ' + $t('settings.search.reembedModalTruncated', { count: preview.total_files })
      : '';
    confirmDialog.request(
      $t('settings.search.reembedModalTitle'),
      $t('settings.search.reembedModalMessage', {
        count: preview.total_files,
        userPhrase: $t('settings.search.userCount', { count: preview.affected_users }),
      }) + truncatedNote,
      reembed,
      { confirmText: $t('settings.search.reembedButton') }
    );
  }

  async function reembed() {
    isReembedding = true;
    try {
      const res = await axiosInstance.post('/search/reembed-degraded');
      if (res.data.status === 'started') {
        toastStore.success($t('settings.search.reembedStarted'));
        isReindexing = true;
        await loadStatus();
      } else if (res.data.status === 'already_running') {
        toastStore.info($t('settings.search.reembedAlreadyRunning'));
      } else if (res.data.status === 'no_degraded_files') {
        toastStore.info($t('settings.search.reembedNoFiles'));
        await loadDegradedPreview();
      }
    } catch (e: unknown) {
      toastStore.error(getErrorMessage(e, $t('settings.search.reembedFailed')));
    } finally {
      isReembedding = false;
    }
  }

  async function loadModels() {
    try {
      const res = await axiosInstance.get('/search/models');
      models = res.data.models;
      currentModelId = res.data.current_model_id;
      selectedModelId = currentModelId;
    } catch (e) {
      console.error('Failed to load models:', e);
    }
  }

  async function loadStatus() {
    try {
      const res = await axiosInstance.get('/search/reindex/status');
      indexStatus = res.data;
      // Check if reindex is currently in progress (from backend status)
      if (indexStatus?.in_progress && !reindexProgress) {
        isReindexing = true;
      }
    } catch (e) {
      console.error('Failed to load index status:', e);
    }
    // While the run state is unknown (Redis unreachable) keep asking; stop once it is known.
    if (destroyed) return;
    if (indexStatus?.status_unavailable) {
      statusPoller.start();
    } else {
      statusPoller.stop();
    }
  }

  function handleModelChange() {
    if (selectedModelId === currentModelId) return;
    confirmDialog.request(
      $t('settings.search.modelChangeTitle'),
      $t('search.modelChangeWarning'),
      switchModel,
      {
        confirmText: $t('settings.search.applyAndReindex'),
        onCancel: () => (selectedModelId = currentModelId),
      }
    );
  }

  // Downloading weights takes minutes, so this reports progress rather than blocking
  // on one long request. The backend calls are idempotent (already_registered /
  // already_deployed), so a retry after a timeout is safe.
  async function prepareModel() {
    if (!selectedModel || selectedModel.ready) return;
    isPreparingModel = true;
    const target = selectedModel.model_id;
    try {
      await axiosInstance.post(`/search/models/neural/${target}/register`);
      await axiosInstance.post(`/search/models/neural/${target}/deploy`);
      await loadModels();
      selectedModelId = target;
      if (models.find(m => m.model_id === target)?.ready) {
        toastStore.success($t('settings.search.modelReady'));
      } else {
        // Deploy returned without error but the model still cannot embed. Reporting
        // success here is what "DEPLOY: COMPLETED then fails to embed" looks like to
        // a user, so say what is actually true.
        toastStore.error($t('settings.search.modelNotReady'));
      }
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toastStore.error(detail || $t('settings.search.modelPrepareFailed'));
    } finally {
      isPreparingModel = false;
    }
  }

  async function switchModel() {
    isSwitchingModel = true;
    try {
      const res = await axiosInstance.post('/search/models', {
        model_id: selectedModelId,
      });
      currentModelId = selectedModelId;
      toastStore.success(res.data.message);
      isReindexing = true;
      await loadStatus();
    } catch (e: unknown) {
      toastStore.error(getErrorMessage(e, $t('search.switchModelFailed')));
      selectedModelId = currentModelId;
    } finally {
      isSwitchingModel = false;
    }
  }

  async function handleReindexAll() {
    isReindexing = true;
    try {
      const res = await axiosInstance.post('/search/reindex');
      toastStore.success(res.data.message);
      await loadStatus();
      // Don't set isReindexing = false here - WebSocket 'reindex-complete' event handles that
    } catch (e: unknown) {
      toastStore.error(getErrorMessage(e, $t('search.reindexStartFailed')));
      isReindexing = false; // Only reset on error
    }
  }

  async function handleReindexPending() {
    isReindexing = true;
    try {
      const res = await axiosInstance.post('/search/reindex?pending_only=true');
      toastStore.success(res.data.message);
      await loadStatus();
      // Don't set isReindexing = false here - WebSocket 'reindex-complete' event handles that
    } catch (e: unknown) {
      toastStore.error(getErrorMessage(e, $t('search.reindexStartFailed')));
      isReindexing = false; // Only reset on error
    }
  }

  async function handleStopReindex() {
    isStopping = true;
    try {
      const res = await axiosInstance.post('/search/reindex/stop');
      if (res.data.status === 'not_running') {
        isReindexing = false;
        isStopping = false;
        reindexProgress = null;
        await loadStatus();
      }
      toastStore.info(res.data.message);
    } catch (e: unknown) {
      toastStore.error(getErrorMessage(e, $t('search.reindexStopFailed')));
      isStopping = false;
    }
  }

  async function loadIndexHealth() {
    try {
      const res = await axiosInstance.get('/search/index-health');
      indexHealth = res.data;
    } catch (e) {
      console.error('Failed to load index health:', e);
      indexHealth = {};
    }
  }

  $: statusUnknown = indexStatus?.status_unavailable === true;

  $: progressPercent = indexStatus
    ? Math.round((indexStatus.indexed_files / Math.max(indexStatus.total_files, 1)) * 100)
    : 0;

  $: modelChanged = selectedModelId !== currentModelId;
  // The switch endpoint refuses an undeployed model (#437), so offering
  // Apply for one produces a 409 the admin can do nothing about.
  $: selectedModelReady = selectedModel?.ready !== false;

  $: selectedModel = models.find(m => m.model_id === selectedModelId);

  // Derive overall health status for the chip
  $: hasRedIndices = indexHealth
    ? Object.values(indexHealth).some(i => i.status === 'red')
    : false;
</script>

{#if isLoading}
  <SettingsSkeleton variant="dashboard" />
{:else}
  <!-- Status Chips Row -->
  {#if indexStatus}
    <div class="status-chips-row">
      <StatusChip
        label={$t('settings.search.chipIndexed')}
        value="{indexStatus.indexed_files}/{indexStatus.total_files}"
        status={progressPercent === 100 ? 'green' : progressPercent > 0 ? 'yellow' : 'neutral'}
      />
      <StatusChip
        label={$t('settings.search.chipModel')}
        value={indexStatus.current_model.split('/').pop() || indexStatus.current_model}
        status="blue"
      />
      <StatusChip
        label={$t('settings.search.chipHealth')}
        value={hasRedIndices ? $t('settings.search.chipHealthNeedsRepair') : $t('settings.search.chipHealthOk')}
        status={hasRedIndices ? 'red' : 'green'}
      />
      {#if indexStatus.pending_files > 0}
        <StatusChip
          label={$t('settings.search.chipPending')}
          value={indexStatus.pending_files !== 1 ? $t('settings.search.chipFileCountPlural', { count: indexStatus.pending_files }) : $t('settings.search.chipFileCount', { count: indexStatus.pending_files })}
          status="yellow"
        />
      {/if}
    </div>
  {/if}

  {#if statusUnknown}
    <div class="banner banner-neutral" role="status">
      <div class="banner-body">{$t('settings.search.statusUnavailable')}</div>
    </div>
  {/if}

  <!-- Live reindex progress -->
  {#if isReindexing}
    {@const hasCounts = !!reindexProgress && reindexProgress.total_files > 0}
    <div class="reindex-card">
      <LiveProgressCard
        title={isStopping ? $t('search.stoppingReindex') : $t('search.reindexingInProgress')}
        state={isStopping ? 'stopping' : hasCounts ? 'running' : 'starting'}
        done={hasCounts ? reindexProgress?.indexed_files ?? null : null}
        total={hasCounts ? reindexProgress?.total_files ?? null : null}
        percent={hasCounts && reindexProgress ? reindexProgress.progress * 100 : null}
        etaSeconds={reindexProgress?.eta_seconds ?? null}
        remainingLabel={$t('upload.remaining')}
        message={hasCounts ? reindexProgress?.message ?? '' : $t('search.reindexStarting')}
        barLabel={$t('search.reindexProgressBar')}
        stopLabel={$t('search.stopReindex')}
        stoppingLabel={$t('search.stoppingReindex')}
        onStop={handleStopReindex}
      />
    </div>
  {:else if indexStatus && indexStatus.total_files > 0 && progressPercent < 100}
    <div class="progress-container">
      <div class="progress-bar-slot">
        <ProgressBar percent={progressPercent} ariaLabel={$t('search.reindexProgressBar')} />
      </div>
      <span class="progress-text">{$t('settings.search.percentIndexed', { percent: progressPercent })}</span>
    </div>
  {/if}

  <!-- Last reindex stats -->
  {#if lastReindexStats && !isReindexing}
    <div class="reindex-stats">
      <div class="stats-header">
        <span class="stats-label">{$t('settings.search.lastReindex')}</span>
        <button class="btn-dismiss" on:click={() => lastReindexStats = null}>&times;</button>
      </div>
      <div class="stats-row">
        <span>{$t('settings.search.filesIndexed', { indexed: lastReindexStats.indexed_files, total: lastReindexStats.total_files })}</span>
        <span>{$t('settings.search.chunksCount', { count: lastReindexStats.total_chunks })}</span>
        <span class="stats-mode {lastReindexStats.mode || 'cpu'}">
          {(lastReindexStats.mode || 'cpu').toUpperCase()}
        </span>
      </div>
      {#if lastReindexStats.failed_files > 0}
        <div class="stats-row error">
          <span>{$t('settings.search.failedCount', { count: lastReindexStats.failed_files })}</span>
        </div>
      {/if}
    </div>
  {/if}

  <!-- Neural search bootstrap self-heal warning (issue #625) -->
  {#if bootstrapStatus?.state === 'degraded'}
    <div class="banner banner-warning">
      <div class="banner-title">{$t('settings.search.bootstrapDegradedTitle')}</div>
      <div class="banner-body">
        {$t('settings.search.bootstrapDegradedBody', { count: bootstrapStatus.attempts })}
      </div>
      {#if bootstrapStatus.last_error}
        <div class="banner-detail">
          {$t('settings.search.bootstrapLastError', { error: bootstrapStatus.last_error })}
        </div>
      {/if}
      {#if bootstrapStatus.retry_at}
        <div class="banner-detail">
          {$t('settings.search.bootstrapRetryAt', { time: formatDateTime(bootstrapStatus.retry_at, '', $locale) })}
        </div>
      {/if}
      {#if (bootstrapStatus.text_only_chunk_files ?? 0) > 0}
        <div class="banner-detail">
          {$t('settings.search.bootstrapTextOnlyFiles', { count: bootstrapStatus.text_only_chunk_files ?? 0 })}
        </div>
      {/if}
    </div>
  {/if}

  <!-- Re-embed degraded (text-only) files (issue #626, the #625 follow-up). Shown
       independently of the bootstrap banner above — a degraded window can leave text-only
       files behind even after the neural pipeline has since recovered. -->
  {#if degradedPreview && degradedPreview.total_files > 0}
    <div class="banner banner-warning reembed-banner">
      <div class="reembed-banner-text">
        <div class="banner-title">{$t('settings.search.reembedButton')}</div>
        <div class="banner-body">
          {$t('settings.search.reembedButtonCount', { count: degradedPreview.total_files })}
        </div>
      </div>
      <button
        class="btn btn-secondary btn-sm reembed-btn"
        on:click={openReembedModal}
        disabled={isReembedding || isCheckingDegraded || isReindexing}
      >
        {#if isReembedding}<Spinner size="small" />{/if}
        {$t('settings.search.reembedButton')}
      </button>
    </div>
  {/if}

  <!-- Embedding Model Selection -->
  <div class="section-divider"></div>
  <div class="subsection-header">
    <h4 class="subsection-title">{$t('search.embeddingModel')}</h4>
    <div class="subsection-actions">
      {#if indexStatus && indexStatus.pending_files > 0 && !isReindexing && !statusUnknown}
        <button
          class="btn btn-secondary btn-sm"
          on:click={handleReindexPending}
          disabled={isReindexing}
        >
          {$t('settings.search.reindexPending')}
        </button>
      {/if}
      {#if indexStatus}
        <button
          class="btn btn-primary btn-sm"
          on:click={handleReindexAll}
          disabled={isReindexing || statusUnknown}
        >
          {isReindexing ? ($t('search.reindexing')) : ($t('search.reindexAll'))}
        </button>
      {/if}
    </div>
  </div>

  <FormField class="embedding-model-field" id="embedding-model-select" label={$t('settings.search.modelLabel')}>
    <select
      id="embedding-model-select"
      class="form-control"
      bind:value={selectedModelId}
      disabled={isSwitchingModel || isReindexing || models.length === 0}
    >
      {#if models.length === 0}
        <option value="">{$t('settings.search.loadingModels')}</option>
      {:else}
        {#each models as model (model.model_id)}
          <option value={model.model_id}>
            {model.name} — {model.dimension}d, ~{model.size_mb}MB{model.model_id === currentModelId ? ' (Current)' : ''}
          </option>
        {/each}
      {/if}
    </select>
    {#if selectedModel}
      <div class="model-language">
        <span
          class="lang-badge"
          class:multilingual={selectedModel.language_type === 'multilingual'}
        >
          {selectedModel.language_type === 'multilingual'
            ? $t('settings.search.multilingualBadge')
            : $t('settings.search.englishOnlyBadge')}
        </span>
        {#if !selectedModel.ready}
          <span class="lang-badge not-ready">{$t('settings.search.notDownloadedBadge')}</span>
        {/if}
      </div>
      <small class="form-text">{selectedModel.description}</small>
      {#if !selectedModel.ready}
        <!-- Without this the only route to a non-default model was two API calls by
             hand: the switch endpoint refuses a model OpenSearch cannot embed with. -->
        <div class="model-prepare">
          <small class="form-text">{$t('settings.search.modelNotDownloaded')}</small>
          <button
            class="btn btn-secondary prepare-btn"
            on:click={prepareModel}
            disabled={isPreparingModel || isSwitchingModel || isReindexing}
          >
            <!-- Indeterminate on purpose: ML Commons reports task STATE, not a
                 download percentage, and a fake percent would be a lie. The spinner
                 says "working"; the toast says how it ended. -->
            {#if isPreparingModel}<Spinner size="small" />{/if}
            {isPreparingModel
              ? $t('settings.search.preparingModel')
              : $t('settings.search.downloadAndDeploy')}
          </button>
        </div>
      {/if}
    {/if}
  </FormField>

  {#if modelChanged}
    <div class="form-actions">
      <button
        class="btn btn-secondary"
        on:click={() => (selectedModelId = currentModelId)}
      >
        {$t('settings.search.cancel')}
      </button>
      <button
        class="btn btn-primary"
        on:click={handleModelChange}
        disabled={isSwitchingModel || !selectedModelReady}
        title={selectedModelReady ? '' : $t('settings.search.modelNotDownloaded')}
      >
        {isSwitchingModel ? $t('settings.search.applying') : $t('settings.search.applyAndReindex')}
      </button>
    </div>
  {/if}

  <!-- Index health moved to Data Integrity section -->
{/if}

<ConfirmationModal
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  confirmText={$confirmLabel}
  variant={$confirmVariant}
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<style>







  .subsection-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    margin-bottom: var(--space-3);
  }

  .subsection-actions {
    display: flex;
    gap: var(--space-2);
    flex-shrink: 0;
  }

  .subsection-title {
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
    margin: 0;
    color: var(--text-color);
  }

  .status-chips-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-bottom: var(--space-3);
  }

  .progress-container {
    display: flex;
    align-items: center;
    gap: var(--space-2-5);
    margin-bottom: var(--space-2);
  }

  .progress-bar-slot {
    flex: 1;
  }

  .progress-text {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    white-space: nowrap;
  }



  :global(.form-field.embedding-model-field) {
    margin-bottom: var(--space-3);
  }

  .form-control {
    padding: var(--space-2) var(--space-2-5);
    font-size: var(--font-size-sm);
  }

  .form-control:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .form-text {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin-top: var(--space-0-5);
  }

  .model-language {
    margin-top: var(--space-1-5);
  }

  /* Colours come from theme vars so light/dark parity is automatic. */
  .lang-badge {
    display: inline-block;
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-xl);
    font-size: 0.7rem;
    font-weight: var(--font-weight-semibold);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    background-color: var(--background-color);
  }

  .lang-badge.multilingual {
    border-color: var(--primary-color);
    color: var(--primary-on-surface);
  }

  .lang-badge.not-ready {
    border-color: var(--warning-color);
    color: var(--warning-color);
  }

  .model-prepare {
    display: flex;
    align-items: flex-start;
    gap: var(--space-3);
    margin-top: var(--space-2);
    flex-wrap: wrap;
  }

  .model-prepare .form-text {
    flex: 1 1 20rem;
  }

  .prepare-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
  }

  .form-actions {
    display: flex;
    gap: var(--space-3);
    margin-top: var(--space-3);
    justify-content: flex-end;
  }

  .form-actions .btn-secondary {
    margin-right: auto;
  }

  .section-divider {
    margin: var(--space-5) 0;
    border-top: 1px solid var(--border-color);
  }

  /* Neural search bootstrap warning banner (#625). Colours come from theme vars so
     light/dark parity is automatic, matching the .lang-badge pattern above. */
  .banner {
    border-radius: var(--radius-md);
    padding: var(--space-2-5) var(--space-3);
    margin-bottom: var(--space-3);
    font-size: var(--font-size-sm);
    line-height: 1.4;
  }

  .banner-warning {
    border: 1px solid var(--warning-color);
    background-color: var(--warning-bg);
    color: var(--text-color);
  }

  .banner-neutral {
    border: 1px solid var(--border-color);
    background-color: var(--surface-color);
    color: var(--text-secondary);
  }

  .banner-title {
    font-weight: var(--font-weight-semibold);
    margin-bottom: var(--space-1);
    color: var(--warning-color);
  }

  .banner-body {
    margin-bottom: var(--space-0-5);
  }

  .banner-detail {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin-top: var(--space-0-5);
  }

  /* Re-embed degraded files banner (#626) — same .banner-warning shell as the bootstrap
     banner above, laid out as text + action button. */
  .reembed-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }

  .reembed-banner-text {
    flex: 1;
    min-width: 0;
  }

  .reembed-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    flex-shrink: 0;
  }

  /* Reindex completion stats */
  .reindex-stats {
    margin-bottom: var(--space-3);
    padding: var(--space-2-5) var(--space-3);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    font-size: var(--font-size-xs);
  }

  .stats-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--space-1-5);
  }

  .stats-label {
    font-weight: var(--font-weight-semibold);
    font-size: var(--font-size-sm);
    color: var(--text-color);
  }

  .btn-dismiss {
    background: none;
    border: none;
    font-size: var(--font-size-base);
    color: var(--text-secondary);
    cursor: pointer;
    padding: 0 var(--space-1);
    line-height: 1;
  }

  .stats-row {
    display: flex;
    gap: var(--space-4);
    color: var(--text-secondary);
    align-items: center;
  }

  .stats-row.error {
    margin-top: var(--space-1);
    color: var(--danger-color);
  }

  .stats-mode {
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-xl);
    font-weight: var(--font-weight-semibold);
    font-size: var(--font-size-2xs);
    text-transform: uppercase;
  }

  .stats-mode.gpu {
    background: rgba(16, 185, 129, 0.12);
    color: #10b981;
  }

  .stats-mode.cpu {
    background: rgba(var(--accent-rgb), 0.12);
    color: var(--primary-on-surface);
  }

  @media (max-width: 768px) {
    .reembed-banner {
      flex-direction: column;
      align-items: stretch;
    }

    .reembed-btn {
      justify-content: center;
    }

    .subsection-header {
      flex-direction: column;
      align-items: flex-start;
    }

    .subsection-actions {
      width: 100%;
      flex-wrap: wrap;
    }

    .subsection-actions .btn {
      flex: 1;
    }

    .form-actions {
      flex-wrap: wrap;
    }

    .form-actions .btn-secondary {
      margin-right: 0;
    }

    .form-actions .btn {
      flex: 1;
      min-height: 44px;
    }

    .subsection-actions .btn {
      min-height: 44px;
    }
  }

  .reindex-card {
    margin-bottom: var(--space-3);
  }
</style>
