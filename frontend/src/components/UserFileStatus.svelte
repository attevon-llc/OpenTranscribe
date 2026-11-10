<script lang="ts">
  import InfoIcon from '$components/ui/InfoIcon.svelte';
  import { SvelteSet, SvelteURLSearchParams } from 'svelte/reactivity';
  import { onMount, onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import type { DetailedFileStatus, UserFileStatusSummary, UserTask } from '$lib/types/fileStatus';
  import axiosInstance from '$lib/axios';
  import { apiCache, cacheKey, CacheTTL } from '$lib/apiCache';
  import { user } from '$stores/auth';
  import { websocketStore } from '$stores/websocket';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage, getErrorStatus } from '$lib/utils/apiError';
  import { createPoller } from '$lib/utils/poller';
  import { getFlowerUrl } from '$lib/utils/url';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import SkeletonLoader from './ui/SkeletonLoader.svelte';
  import TaskFilterPanel from '$components/fileStatus/TaskFilterPanel.svelte';
  import TasksGrid from '$components/fileStatus/TasksGrid.svelte';
  import FileDetailModal from '$components/fileStatus/FileDetailModal.svelte';
  import Banner from '$components/ui/Banner.svelte';

  // Flower exposes task arguments (file/user IDs) and worker topology, so the
  // entry is admin-only. Cosmetic only — nginx auth_request is the real gate.
  $: isAdmin = $user?.role === 'admin' || $user?.role === 'super_admin';
  // A deployment may not run Flower at all; its auth probe then denies everyone.
  $: flowerEnabled = isCapabilityEnabled($capabilities, 'admin.flower');

  // Component state
  let loading = false;
  let error: string | null = null;
  let fileStatus: UserFileStatusSummary | null = null;
  let selectedFile: string | null = null;
  let detailedStatus: DetailedFileStatus | null = null;
  let retryingFiles = new SvelteSet<string>();

  // Auto-refresh settings (enabled by default)
  // WebSocket push handles real-time updates; this is a fallback safety net to catch any
  // missed notifications, every 2 minutes.
  const refreshPoller = createPoller(
    () => Promise.all([fetchFileStatus(true), showTasksSection ? fetchTasks(true) : undefined]),
    { intervalMs: 120000 }
  );

  // Tasks section state
  let tasks: UserTask[] = [];
  let tasksLoading = false;
  let tasksError: string | null = null;
  let showTasksSection = false;

  // Restore tasks section state from session storage
  if (typeof window !== 'undefined') {
    const savedTasksSection = sessionStorage.getItem('showTasksSection');
    if (savedTasksSection === 'true') {
      showTasksSection = true;
    }
  }

  // Task filtering
  let taskFilter = 'all'; // 'all', 'pending', 'in_progress', 'completed', 'failed'
  let taskTypeFilter = 'all'; // 'all', 'transcription', 'summarization'
  let taskAgeFilter = 'all'; // 'all', 'today', 'week', 'month', 'older'
  let taskDateFrom = '';
  let taskDateTo = '';
  let filteredTasks: UserTask[] = [];

  // Task pagination state
  let taskPage = 1;
  const taskPageSize = 25;
  let taskTotalPages = 0;
  let filtersReady = false;

  // WebSocket subscription
  let unsubscribeWebSocket: (() => void) | null = null;
  let lastProcessedNotificationId = '';

  // Push-based cache invalidation listener
  function handleCacheInvalidation(event: Event) {
    const scope = (event as CustomEvent).detail?.scope;
    if (scope === 'files' || scope === 'all') {
      void fetchFileStatus(true);
    }
  }

  onMount(() => {
    void fetchFileStatus();
    setupWebSocketUpdates();
    refreshPoller.start();

    // Listen for push-based cache invalidation from WebSocket
    window.addEventListener('cache-invalidated', handleCacheInvalidation);

    // Always load tasks on mount
    if (tasks.length === 0) {
      void fetchTasks();
    }
  });

  // Refetch tasks when filters change (reset to page 1)
  // Only a change since the last fetch refetches: `filtersReady` flips true right AFTER the first
  // fetch, and the block used to re-run on that flip and request the identical page again.
  $: tasksFilterKey = [taskFilter, taskTypeFilter, taskAgeFilter, taskDateFrom, taskDateTo].join('|');
  let fetchedFilterKey: string | null = null;
  function refetchOnFilterChange(filterKey: string) {
    if (!filtersReady || filterKey === fetchedFilterKey) return;
    taskPage = 1;
    void fetchTasks(true);
  }

  $: refetchOnFilterChange(tasksFilterKey);

  async function fetchFileStatus(silent = false) {
    if (!silent) {
      loading = true;
    }
    error = null;

    try {
      if (silent) {
        // Silent refreshes bypass cache to get fresh data
        apiCache.invalidate('status:');
      }
      fileStatus = await apiCache.getOrFetch(
        cacheKey.status(),
        async () => {
          const response = await axiosInstance.get('/my-files/status');
          return response.data;
        },
        CacheTTL.STATUS
      );
    } catch (err: unknown) {
      console.error('Error fetching file status:', err);
      if (!silent) {
        error = getErrorMessage(err, $t('fileStatus.loadFailed'));
      }
    } finally {
      if (!silent) {
        loading = false;
      }
    }
  }

  async function fetchTasks(silent = false) {
    if (!silent) {
      tasksLoading = true;
    }
    tasksError = null;
    fetchedFilterKey = tasksFilterKey;

    try {
      // Build query parameters for backend filtering + pagination
      const params = new SvelteURLSearchParams();
      if (taskFilter !== 'all') {
        params.append('status', taskFilter);
      }
      if (taskTypeFilter !== 'all') {
        params.append('task_type', taskTypeFilter);
      }
      if (taskAgeFilter !== 'all') {
        params.append('age_filter', taskAgeFilter);
      }
      if (taskDateFrom) {
        params.append('date_from', taskDateFrom);
      }
      if (taskDateTo) {
        params.append('date_to', taskDateTo);
      }
      params.append('page', taskPage.toString());
      params.append('page_size', taskPageSize.toString());

      const response = await axiosInstance.get(`/tasks?${params.toString()}`);
      const data = response.data;

      // Handle paginated response
      if (data.items) {
        tasks = data.items;
        taskTotalPages = data.total_pages;
      } else {
        // Fallback for non-paginated response
        tasks = Array.isArray(data) ? data : [];
        taskTotalPages = 1;
      }
      filteredTasks = tasks;
      filtersReady = true;
      // A filter changed while this first load was in flight was ignored (not ready yet).
      refetchOnFilterChange(tasksFilterKey);
    } catch (err: unknown) {
      console.error('Error fetching tasks:', err);
      if (!silent) {
        tasksError = getErrorMessage(err, $t('fileStatus.tasksLoadFailed'));
      }
    } finally {
      if (!silent) {
        tasksLoading = false;
      }
    }
  }

  function openFlowerDashboard() {
    // Same-origin path; the proxy in front of Flower supplies its credentials.
    const url = getFlowerUrl();
    // 'noopener' — unlike <a target="_blank">, window.open() gets no implicit
    // noopener, so the new tab would keep a live window.opener handle back here.
    window.open(url, '_blank', 'noopener');
  }

  async function fetchDetailedStatus(fileId: string) {
    try {
      const response = await axiosInstance.get(`/my-files/${fileId}/status`);
      detailedStatus = response.data;
      selectedFile = fileId;
      lockScroll();
    } catch (err: unknown) {
      console.error('Error fetching detailed status:', err);
      error = getErrorMessage(err, $t('fileStatus.detailsLoadFailed'));
    }
  }

  function closeModal() {
    detailedStatus = null;
    selectedFile = null;
    unlockScroll();
  }

  async function retryFile(fileId: string) {
    if (retryingFiles.has(fileId)) return;

    retryingFiles.add(fileId);
    retryingFiles = retryingFiles; // Trigger reactivity

    try {
      await axiosInstance.post(`/my-files/${fileId}/retry`);

      // Refresh status after retry
      await fetchFileStatus(true); // Silent refresh
      if (selectedFile === fileId) {
        await fetchDetailedStatus(fileId);
      }

      // Show success message
      showMessage($t('fileStatus.retryInitiated'), 'success');

    } catch (err: unknown) {
      console.error('Error retrying file:', err);
      const errorMsg = getErrorMessage(err, $t('fileStatus.retryFailed'));
      // Issue #788: the retry-spam guard (`POST /my-files/{uuid}/retry`) is
      // itself rate-limited. Rather than a plain error toast, offer the
      // "Retry" action right there -- disabled until the window elapses
      // (Toast.svelte drives that off `retryAfterSeconds`), so the user does
      // not have to leave the toast to try the same button again. Absent or
      // unparseable Retry-After (no `retryAfterSeconds` on the error) falls
      // back to today's plain error toast.
      const retryAfterSeconds =
        getErrorStatus(err) === 429
          ? (err as { retryAfterSeconds?: number })?.retryAfterSeconds
          : undefined;
      if (typeof retryAfterSeconds === 'number') {
        toastStore.warning(errorMsg, undefined, {
          retryAfterSeconds,
          action: { label: $t('fileStatus.retry'), onClick: () => void retryFile(fileId) },
        });
      } else {
        showMessage(errorMsg, 'error');
      }
    } finally {
      retryingFiles.delete(fileId);
      retryingFiles = retryingFiles; // Trigger reactivity
    }
  }

  async function requestRecovery() {
    loading = true;

    try {
      await axiosInstance.post('/my-files/request-recovery');
      showMessage($t('fileStatus.recoveryInitiated'), 'success');

      // Refresh status after a delay
      setTimeout(() => {
        void fetchFileStatus(true); // Silent refresh
      }, 2000);

    } catch (err: unknown) {
      console.error('Error requesting recovery:', err);
      const errorMsg = getErrorMessage(err, $t('fileStatus.recoveryFailed'));
      showMessage(errorMsg, 'error');
    } finally {
      loading = false;
    }
  }

  function showMessage(message: string, type: 'success' | 'error') {
    if (type === 'success') {
      toastStore.success(message);
    } else {
      toastStore.error(message);
    }
  }

  // Note: formatFileAge is now handled by the backend - use formatted_file_age field

  // Note: formatDate (presentational) now lives in FileDetailModal.svelte

  // Note: formatDuration is now handled by the backend - use formatted_duration field

  // Note: formatFileSize is now handled by the backend - use formatted_file_size field

  // Note: getStatusBadgeClass is now handled by the backend - use status_badge_class field

  // Filtering is now handled by the backend

  // Setup WebSocket updates for real-time file status changes
  function setupWebSocketUpdates() {
    unsubscribeWebSocket = websocketStore.subscribe(($ws) => {
      if ($ws.notifications.length > 0) {
        const latestNotification = $ws.notifications[0];

        // Only process if this is a new notification we haven't handled
        if (latestNotification.id !== lastProcessedNotificationId) {
          lastProcessedNotificationId = latestNotification.id;

          // Check if this notification is for transcription status
          if (latestNotification.type === 'transcription_status' && latestNotification.data?.file_id) {

            // Refresh file status when we get updates
            void fetchFileStatus(true); // Silent refresh

            // Also refresh tasks if tasks section is open
            if (showTasksSection) {
              void fetchTasks(true); // Silent refresh
            }
          }
        }
      }
    });
  }

  // Cleanup on component destroy
  onDestroy(() => {
    refreshPoller.stop();
    if (unsubscribeWebSocket) {
      unsubscribeWebSocket();
    }
    window.removeEventListener('cache-invalidated', handleCacheInvalidation);
    if (selectedFile !== null) unlockScroll();
  });
</script>

<div class="file-status-container">
  <slot name="top" />
  <div class="header">
    <h2>{$t('fileStatus.title')}</h2>
    <div class="controls">
      <span class="live-status-icon" data-tooltip={$t('fileStatus.liveUpdatesTooltip')}>
        <InfoIcon size={14} />
      </span>

      {#if isAdmin && flowerEnabled}
        <button
          class="flower-btn"
          on:click={openFlowerDashboard}
          title={$t('fileStatus.flowerTooltip')}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
          </svg>
          {$t('nav.flowerDashboard')}
        </button>
      {/if}

      {#if fileStatus?.has_problems}
        <button
          class="recovery-btn"
          on:click={requestRecovery}
          disabled={loading}
          title={$t('fileStatus.requestRecoveryAll')}
        >
          {$t('fileStatus.requestRecoveryAll')}
        </button>
      {/if}
    </div>
  </div>

  {#if error}
    <Banner tone="error">
      {error}
    </Banner>
  {/if}

  {#if loading && !fileStatus}
    <div class="skeleton-status">
      <div class="status-cards">
        {#each Array(5) as _, i (i)}
          <div class="status-card skeleton-card">
            <SkeletonLoader lines={1} height={28} />
            <SkeletonLoader lines={1} height={12} />
          </div>
        {/each}
      </div>
      <div class="skeleton-table">
        <SkeletonLoader lines={6} height={36} />
      </div>
    </div>
  {:else if fileStatus}
    <div class="status-overview">
      <div class="status-cards">
        <div class="status-card">
          <div class="status-number">{fileStatus.status_counts.total}</div>
          <div class="status-label">{$t('fileStatus.totalFiles')}</div>
        </div>

        <div class="status-card">
          <div class="status-number">{fileStatus.status_counts.completed}</div>
          <div class="status-label">{$t('common.completed')}</div>
        </div>

        <div class="status-card">
          <div class="status-number">{fileStatus.status_counts.processing}</div>
          <div class="status-label">{$t('common.processing')}</div>
        </div>

        <div class="status-card">
          <div class="status-number">{fileStatus.status_counts.pending}</div>
          <div class="status-label">{$t('common.pending')}</div>
        </div>

        <div class="status-card error">
          <div class="status-number">{fileStatus.status_counts.error}</div>
          <div class="status-label">{$t('fileStatus.errors')}</div>
        </div>
      </div>

    </div>
  {/if}

  <!-- Unified Tasks Section (always visible) -->
  <div class="tasks-section">
    <TaskFilterPanel
      bind:taskFilter
      bind:taskTypeFilter
      bind:taskAgeFilter
      bind:taskDateFrom
      bind:taskDateTo
      {fileStatus}
    />

    <TasksGrid
      {tasks}
      {filteredTasks}
      {tasksLoading}
      {tasksError}
      {taskFilter}
      {taskTypeFilter}
      {taskPage}
      {taskTotalPages}
      on:viewDetails={(e) => fetchDetailedStatus(e.detail)}
      on:pageChange={(e) => { taskPage = e.detail; void fetchTasks(true); }}
    />
  </div>

  <FileDetailModal
    {detailedStatus}
    {selectedFile}
    {retryingFiles}
    on:close={closeModal}
    on:retry={(e) => retryFile(e.detail)}
  />
</div>

<style>
  .file-status-container {
    max-width: 1200px;
    margin: 0 auto;
    padding: var(--space-4);
    color: var(--text-color);
    height: calc(100vh - var(--content-top));
    height: calc(100dvh - var(--content-top));
    overflow-y: auto;
  }

  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--space-8);
  }

  .header h2 {
    margin: 0;
    color: var(--text-color);
  }

  .controls {
    display: flex;
    gap: var(--space-4);
    align-items: center;
  }

  .recovery-btn, .flower-btn {
    padding: 0.6rem 1.2rem;
    background: var(--primary-color);
    color: var(--text-on-solid);
    border: none;
    border-radius: var(--radius-xl);
    cursor: pointer;
    transition: all var(--duration-base) ease;
    font-weight: var(--font-weight-medium);
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: 0.95rem;
    box-shadow: var(--shadow-primary);
  }

  .flower-btn {
    background: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
  }

  .recovery-btn:hover {
    background: var(--primary-color);
    transform: scale(1.02);
    box-shadow: var(--shadow-primary-hover);
  }

  .recovery-btn:active {
    transform: scale(1);
  }

  .flower-btn:hover {
    background: var(--button-hover);
    border-color: var(--border-hover);
  }

  .recovery-btn:disabled {
    background: var(--text-light);
    cursor: not-allowed;
    transform: none;
  }

  .live-status-icon {
    position: relative;
    display: flex;
    align-items: center;
    color: var(--text-secondary);
    opacity: 0.5;
    cursor: help;
  }

  .live-status-icon:hover {
    opacity: 0.8;
  }

  .live-status-icon::after {
    content: attr(data-tooltip);
    position: absolute;
    top: calc(100% + 8px);
    right: 0;
    background: rgba(0, 0, 0, 0.85);
    color: var(--text-on-solid);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-regular);
    padding: var(--space-1-5) var(--space-2-5);
    border-radius: var(--radius-md);
    white-space: nowrap;
    pointer-events: none;
    opacity: 0;
    transition: opacity 0.08s ease;
    z-index: 20;
    line-height: 1.3;
  }

  .live-status-icon:hover::after {
    opacity: 1;
  }

  :global([data-theme='dark']) .live-status-icon::after {
    background: rgba(255, 255, 255, 0.92);
    color: #111;
  }

  .status-cards {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: var(--space-3);
    margin: 0 auto var(--space-3) auto;
    max-width: 700px;
  }

  .status-card {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    padding: var(--space-4);
    text-align: center;
    box-shadow: var(--shadow-sm);
    transition: all var(--duration-base) ease;
  }

  .status-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
  }

  .status-card.error {
    border-color: var(--error-color);
    background: var(--error-background);
  }

  :global([data-theme='dark']) .status-card {
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
  }

  :global([data-theme='dark']) .status-card:hover {
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.4);
  }

  .status-number {
    font-size: var(--font-size-2xl);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin-bottom: var(--space-1);
  }

  .status-label {
    /* These labels sit on per-status tinted cards; on the error tint --text-light
     * (an alias of --text-secondary) measures 4.16:1, under WCAG AA. */
    color: var(--text-on-tint);
    font-size: 0.8rem;
    font-weight: var(--font-weight-medium);
  }

  @media (max-width: 768px) {
    .header {
      flex-direction: column;
      align-items: flex-start;
      gap: var(--space-4);
    }

    .controls {
      width: 100%;
      flex-wrap: wrap;
      gap: var(--space-2);
    }

    .status-cards {
      grid-template-columns: repeat(3, 1fr);
      gap: var(--space-2);
    }

    .status-card {
      padding: 0.6rem;
    }

    .status-number {
      font-size: var(--font-size-xl);
    }

    .status-label {
      font-size: 0.7rem;
    }
  }

  @media (max-width: 380px) {
    .status-cards {
      grid-template-columns: repeat(2, 1fr);
    }
  }

  /* Skeleton loading state */
  .skeleton-status {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
  }

  .skeleton-card {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .skeleton-table {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    padding: var(--space-4) var(--space-6);
  }


</style>
