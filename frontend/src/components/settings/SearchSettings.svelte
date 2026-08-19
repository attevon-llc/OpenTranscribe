<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { toastStore } from '$stores/toast';
  import ConfirmationModal from '../ConfirmationModal.svelte';
  import StatusChip from './StatusChip.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import ProgressBar from '../ui/ProgressBar.svelte';

  let showModelChangeModal = false;

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
    current_model: string;
    current_dimension: number;
    last_indexed_at: string | null;
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

  // Live reindex progress
  let reindexProgress: ReindexProgress | null = null;

  function formatEta(seconds: number | null | undefined): string {
    if (seconds == null || seconds <= 0) return '';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }

  // Event handlers for WebSocket events
  function handleReindexProgress(event: CustomEvent<ReindexProgress>) {
    reindexProgress = event.detail;
    isReindexing = true;
  }

  let lastReindexStats: any = null;

  function handleReindexComplete(event: CustomEvent<{ stats: any }>) {
    reindexProgress = null;
    isReindexing = false;
    lastReindexStats = event.detail?.stats || null;
    // Reload status to get updated counts
    loadStatus();
    toastStore.success($t('search.reindexComplete'));
  }

  function handleReindexStopped(event: CustomEvent<{ stats: any; reason: string }>) {
    reindexProgress = null;
    isReindexing = false;
    isStopping = false;
    loadStatus();
    toastStore.success($t('search.reindexStopped'));
  }

  onMount(async () => {
    await Promise.all([loadModels(), loadStatus(), loadIndexHealth()]);
    isLoading = false;

    // Listen for WebSocket events
    window.addEventListener('reindex-progress', handleReindexProgress as EventListener);
    window.addEventListener('reindex-complete', handleReindexComplete as EventListener);
    window.addEventListener('reindex-stopped', handleReindexStopped as EventListener);
  });

  onDestroy(() => {
    window.removeEventListener('reindex-progress', handleReindexProgress as EventListener);
    window.removeEventListener('reindex-complete', handleReindexComplete as EventListener);
    window.removeEventListener('reindex-stopped', handleReindexStopped as EventListener);
  });

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
  }

  function handleModelChange() {
    if (selectedModelId === currentModelId) return;
    showModelChangeModal = true;
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

  async function confirmModelChange() {
    showModelChangeModal = false;
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

  function cancelModelChange() {
    showModelChangeModal = false;
    selectedModelId = currentModelId;
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

  function formatDate(dateStr: string | null): string {
    if (!dateStr) return 'Never';
    try {
      return new Date(dateStr).toLocaleString();
    } catch {
      return dateStr;
    }
  }

  $: progressPercent = indexStatus
    ? Math.round((indexStatus.indexed_files / Math.max(indexStatus.total_files, 1)) * 100)
    : 0;

  $: modelChanged = selectedModelId !== currentModelId;
  // The switch endpoint refuses an undeployed model (#437), so offering
  // Apply for one produces a 409 the admin can do nothing about.
  $: selectedModelReady = selectedModel?.ready !== false;

  $: selectedModel = models.find(m => m.model_id === selectedModelId);

  // Derive overall health status for the chip
  $: healthAllGreen = indexHealth
    ? Object.values(indexHealth).every(i => i.status === 'green')
    : true;
  $: hasRedIndices = indexHealth
    ? Object.values(indexHealth).some(i => i.status === 'red')
    : false;
</script>

{#if isLoading}
  <div class="status-chips-row">
    <div class="skeleton-chip"></div>
    <div class="skeleton-chip wide"></div>
    <div class="skeleton-chip"></div>
  </div>
  <div class="skeleton-bar"></div>
  <div class="skeleton-section">
    <div class="skeleton-line title"></div>
    <div class="skeleton-line full"></div>
  </div>
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

  <!-- Live reindex progress -->
  {#if isReindexing}
    <div class="reindex-live-progress">
      <div class="reindex-header">
        <span class="reindex-label">
          <Spinner size="small" />
          {isStopping
            ? ($t('search.stoppingReindex'))
            : ($t('search.reindexingInProgress'))}
        </span>
        {#if reindexProgress && reindexProgress.total_files > 0}
          <span class="reindex-count">
            {reindexProgress.indexed_files} / {reindexProgress.total_files}
          </span>
        {/if}
      </div>
      {#if reindexProgress && reindexProgress.total_files > 0}
        <div class="progress-container">
          <div class="progress-bar reindexing">
            <div class="progress-fill" style="width: {Math.min(Math.round(reindexProgress.progress * 100), 100)}%"></div>
          </div>
          <span class="progress-text">
            {Math.min(Math.round(reindexProgress.progress * 100), 100)}%
          </span>
        </div>
        <div class="reindex-details">
          {#if reindexProgress.message}
            <span class="reindex-message">{reindexProgress.message}</span>
          {/if}
          {#if formatEta(reindexProgress.eta_seconds)}
            <span class="reindex-eta">{formatEta(reindexProgress.eta_seconds)} {$t('upload.remaining')}</span>
          {/if}
        </div>
      {:else}
        <div class="progress-container indeterminate-row">
          <div class="progress-bar-wrapper">
            <ProgressBar percent={null} />
          </div>
          <span class="progress-text">{$t('search.reindexStarting')}</span>
        </div>
      {/if}
      <div class="reindex-actions">
        <button
          class="btn btn-danger-outline btn-sm"
          on:click={handleStopReindex}
          disabled={isStopping}
        >
          {isStopping ? ($t('search.stoppingReindex')) : ($t('search.stopReindex'))}
        </button>
      </div>
    </div>
  {:else if indexStatus && indexStatus.total_files > 0 && progressPercent < 100}
    <div class="progress-container">
      <div class="progress-bar">
        <div class="progress-fill" style="width: {progressPercent}%"></div>
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

  <!-- Embedding Model Selection -->
  <div class="section-divider"></div>
  <div class="subsection-header">
    <h4 class="subsection-title">{$t('search.embeddingModel')}</h4>
    <div class="subsection-actions">
      {#if indexStatus && indexStatus.pending_files > 0 && !isReindexing}
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
          disabled={isReindexing}
        >
          {isReindexing ? ($t('search.reindexing')) : ($t('search.reindexAll'))}
        </button>
      {/if}
    </div>
  </div>

  <div class="form-group">
    <label for="embedding-model-select">{$t('settings.search.modelLabel')}</label>
    <select
      id="embedding-model-select"
      class="form-control"
      bind:value={selectedModelId}
      disabled={isSwitchingModel || isReindexing || models.length === 0}
    >
      {#if models.length === 0}
        <option value="">{$t('settings.search.loadingModels')}</option>
      {:else}
        {#each models as model}
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
            class="btn btn-secondary"
            on:click={prepareModel}
            disabled={isPreparingModel || isSwitchingModel || isReindexing}
          >
            {isPreparingModel
              ? $t('settings.search.preparingModel')
              : $t('settings.search.downloadAndDeploy')}
          </button>
        </div>
      {/if}
    {/if}
  </div>

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
  isOpen={showModelChangeModal}
  title={$t('settings.search.modelChangeTitle')}
  message={$t('search.modelChangeWarning')}
  confirmText={$t('settings.search.applyAndReindex')}
  on:confirm={confirmModelChange}
  on:cancel={cancelModelChange}
  on:close={cancelModelChange}
/>


<style>
  .skeleton-chip {
    height: 28px;
    width: 90px;
    border-radius: 14px;
    background: var(--border-color, #e5e7eb);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-chip.wide {
    width: 130px;
  }

  .skeleton-bar {
    height: 6px;
    width: 100%;
    border-radius: 3px;
    background: var(--border-color, #e5e7eb);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
    margin-bottom: 1rem;
  }

  .skeleton-section {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .skeleton-line {
    height: 14px;
    border-radius: 4px;
    background: var(--border-color, #e5e7eb);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-line.title {
    width: 40%;
    height: 18px;
  }

  .skeleton-line.full {
    width: 100%;
    height: 36px;
    border-radius: 6px;
  }

  .subsection-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.75rem;
  }

  .subsection-actions {
    display: flex;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .subsection-title {
    font-size: 0.9375rem;
    font-weight: 600;
    margin: 0;
    color: var(--text-color);
  }

  .status-chips-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
  }

  .progress-container {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    margin-bottom: 0.5rem;
  }

  .progress-bar {
    flex: 1;
    height: 6px;
    background: var(--border-color);
    border-radius: 3px;
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    background: #3b82f6;
    border-radius: 3px;
    transition: width 0.3s;
  }

  .progress-text {
    font-size: 0.75rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
    margin-bottom: 0.75rem;
  }

  .form-group label {
    font-weight: 500;
    color: var(--text-color);
    font-size: 0.8125rem;
  }

  .form-control {
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    transition: border-color 0.15s, box-shadow 0.15s;
  }

  .form-control:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px var(--primary-light);
  }

  .form-control:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    background-color: var(--background-color);
  }

  .form-text {
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin-top: 0.125rem;
  }

  .model-language {
    margin-top: 0.375rem;
  }

  /* Colours come from theme vars so light/dark parity is automatic. */
  .lang-badge {
    display: inline-block;
    padding: 0.125rem 0.5rem;
    border-radius: 10px;
    font-size: 0.7rem;
    font-weight: 600;
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    background-color: var(--background-color);
  }

  .lang-badge.multilingual {
    border-color: var(--primary-color);
    color: var(--primary-color);
  }

  .lang-badge.not-ready {
    border-color: var(--warning-color, #b45309);
    color: var(--warning-color, #b45309);
  }

  .model-prepare {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    margin-top: 0.5rem;
    flex-wrap: wrap;
  }

  .model-prepare .form-text {
    flex: 1 1 20rem;
  }

  .form-actions {
    display: flex;
    gap: 0.75rem;
    margin-top: 0.75rem;
    justify-content: flex-end;
  }

  .form-actions .btn-secondary {
    margin-right: auto;
  }

  .section-divider {
    margin: 1.25rem 0;
    border-top: 1px solid var(--border-color);
  }

  /* Live reindex progress styles */
  .reindex-live-progress {
    margin-bottom: 0.75rem;
    padding: 0.75rem;
    background: var(--background-color);
    border-radius: 6px;
    border: 1px solid var(--border-color);
  }

  .reindex-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .reindex-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--primary-color);
  }

  .reindex-count {
    font-size: 0.75rem;
    color: var(--text-secondary);
    font-weight: 500;
  }

  .progress-bar.reindexing {
    background: var(--primary-light, rgba(var(--primary-color-rgb), 0.2));
  }

  .progress-bar.reindexing .progress-fill {
    background: #3b82f6;
    animation: pulse 1.5s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% {
      opacity: 1;
    }
    50% {
      opacity: 0.7;
    }
  }

  .reindex-details {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 0.25rem;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

  .reindex-message {
    flex: 1;
  }

  .reindex-eta {
    white-space: nowrap;
    font-weight: 500;
    color: var(--primary-color);
  }

  .reindex-actions {
    display: flex;
    justify-content: flex-end;
    margin-top: 0.5rem;
  }

  /* Reindex completion stats */
  .reindex-stats {
    margin-bottom: 0.75rem;
    padding: 0.625rem 0.75rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    font-size: 0.75rem;
  }

  .stats-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.375rem;
  }

  .stats-label {
    font-weight: 600;
    font-size: 0.8125rem;
    color: var(--text-color);
  }

  .btn-dismiss {
    background: none;
    border: none;
    font-size: 1rem;
    color: var(--text-secondary);
    cursor: pointer;
    padding: 0 0.25rem;
    line-height: 1;
  }

  .stats-row {
    display: flex;
    gap: 1rem;
    color: var(--text-secondary);
    align-items: center;
  }

  .stats-row.error {
    margin-top: 0.25rem;
    color: var(--danger-color, #dc2626);
  }

  .stats-mode {
    padding: 0.125rem 0.5rem;
    border-radius: 10px;
    font-weight: 600;
    font-size: 0.6875rem;
    text-transform: uppercase;
  }

  .stats-mode.gpu {
    background: rgba(16, 185, 129, 0.12);
    color: #10b981;
  }

  .stats-mode.cpu {
    background: rgba(59, 130, 246, 0.12);
    color: var(--primary-color);
  }

  .btn-danger-outline {
    background: transparent;
    color: var(--danger-color, #dc2626);
    border: 1px solid var(--danger-color, #dc2626);
  }

  .btn-danger-outline:hover:not(:disabled) {
    background: var(--danger-bg, rgba(220, 38, 38, 0.08));
  }

  .btn-danger-outline:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-sm {
    padding: 0.25rem 0.75rem;
    font-size: 0.75rem;
  }

  .indeterminate-row {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    margin-bottom: 0.5rem;
  }

  .indeterminate-row .progress-bar-wrapper {
    flex: 1;
  }

  @media (max-width: 768px) {
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
</style>
