<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import axiosInstance from '../lib/axios';
  import { apiCache, cacheKey, CacheTTL } from '$lib/apiCache';
  import { user } from '../stores/auth';
  import { websocketStore } from '../stores/websocket';
  import { toastStore } from '../stores/toast';
  import { t } from '../stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { getFlowerUrl } from '$lib/utils/url';
  import SkeletonLoader from './ui/SkeletonLoader.svelte';
  import TaskFilterPanel from '$components/fileStatus/TaskFilterPanel.svelte';
  import TasksGrid from '$components/fileStatus/TasksGrid.svelte';
  import FileDetailModal from '$components/fileStatus/FileDetailModal.svelte';

  // Flower exposes task arguments (file/user IDs) and worker topology, so the
  // entry is admin-only. Cosmetic only — nginx auth_request is the real gate.
  $: isAdmin = $user?.role === 'admin' || $user?.role === 'super_admin';

  // Component state
  let loading = false;
  let error: any = null;
  let fileStatus: any = null;
  let selectedFile: any = null;
  let detailedStatus: any = null;
  let retryingFiles = new Set();

  // Auto-refresh settings (enabled by default)
  let refreshInterval: any = null;

  // Tasks section state
  let tasks: any[] = [];
  let tasksLoading = false;
  let tasksError: any = null;
  let showTasksSection = false;

  // Collapsible sections state
  let showProblemsSection = true;  // Expanded by default (needs attention!)
  let showRecentSection = true;    // Expanded by default

  // Restore tasks section state from session storage
  if (typeof window !== 'undefined') {
    const savedTasksSection = sessionStorage.getItem('showTasksSection');
    if (savedTasksSection === 'true') {
      showTasksSection = true;
    }

    // Restore problems section state
    const savedProblemsSection = sessionStorage.getItem('showProblemsSection');
    if (savedProblemsSection !== null) {
      showProblemsSection = savedProblemsSection === 'true';
    }

    // Restore recent section state
    const savedRecentSection = sessionStorage.getItem('showRecentSection');
    if (savedRecentSection !== null) {
      showRecentSection = savedRecentSection === 'true';
    }
  }

  // Task filtering
  let taskFilter = 'all'; // 'all', 'pending', 'in_progress', 'completed', 'failed'
  let taskTypeFilter = 'all'; // 'all', 'transcription', 'summarization'
  let taskAgeFilter = 'all'; // 'all', 'today', 'week', 'month', 'older'
  let taskDateFrom = '';
  let taskDateTo = '';
  let filteredTasks: any[] = [];

  // Task pagination state
  let taskPage = 1;
  let taskPageSize = 25;
  let taskTotal = 0;
  let taskTotalPages = 0;
  let filtersReady = false;

  // WebSocket subscription
  let unsubscribeWebSocket: any = null;
  let lastProcessedNotificationId = '';

  // Push-based cache invalidation listener
  function handleCacheInvalidation(event: Event) {
    const scope = (event as CustomEvent).detail?.scope;
    if (scope === 'files' || scope === 'all') {
      fetchFileStatus(true);
    }
  }

  onMount(() => {
    fetchFileStatus();
    setupWebSocketUpdates();
    startAutoRefresh();

    // Listen for push-based cache invalidation from WebSocket
    window.addEventListener('cache-invalidated', handleCacheInvalidation);

    // Always load tasks on mount
    if (tasks.length === 0) {
      fetchTasks();
    }
  });

  // Refetch tasks when filters change (reset to page 1)
  $: if (filtersReady && (taskFilter || taskTypeFilter || taskAgeFilter || taskDateFrom || taskDateTo)) {
    taskPage = 1;
    fetchTasks(true);
  }

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

    try {
      // Build query parameters for backend filtering + pagination
      const params = new URLSearchParams();
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
        taskTotal = data.total;
        taskTotalPages = data.total_pages;
      } else {
        // Fallback for non-paginated response
        tasks = Array.isArray(data) ? data : [];
        taskTotal = tasks.length;
        taskTotalPages = 1;
      }
      filteredTasks = tasks;
      filtersReady = true;
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

  function toggleTasksSection() {
    showTasksSection = !showTasksSection;

    // Save state to session storage
    if (typeof window !== 'undefined') {
      sessionStorage.setItem('showTasksSection', showTasksSection.toString());
    }

    if (showTasksSection && tasks.length === 0) {
      fetchTasks();
    }
  }

  function toggleProblemsSection() {
    showProblemsSection = !showProblemsSection;
    if (typeof window !== 'undefined') {
      sessionStorage.setItem('showProblemsSection', showProblemsSection.toString());
    }
  }

  function toggleRecentSection() {
    showRecentSection = !showRecentSection;
    if (typeof window !== 'undefined') {
      sessionStorage.setItem('showRecentSection', showRecentSection.toString());
    }
  }

  function openFlowerDashboard() {
    // Dynamically construct Flower URL from current location
    const url = getFlowerUrl();
    // 'noopener' — unlike <a target="_blank">, window.open() gets no implicit
    // noopener, so the new tab would keep a live window.opener handle back here.
    window.open(url, '_blank', 'noopener');
  }

  async function fetchDetailedStatus(fileId: any) {
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

  async function retryFile(fileId: any) {
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
      showMessage(errorMsg, 'error');
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
        fetchFileStatus(true); // Silent refresh
      }, 2000);

    } catch (err: unknown) {
      console.error('Error requesting recovery:', err);
      const errorMsg = getErrorMessage(err, $t('fileStatus.recoveryFailed'));
      showMessage(errorMsg, 'error');
    } finally {
      loading = false;
    }
  }

  function startAutoRefresh() {
    // WebSocket push handles real-time updates; this is a fallback safety net
    // to catch any missed notifications (runs every 2 minutes instead of 30s)
    refreshInterval = setInterval(() => {
      fetchFileStatus(true); // Silent refresh
      if (showTasksSection) {
        fetchTasks(true); // Silent refresh
      }
    }, 120000); // Fallback refresh every 2 minutes
  }

  function showMessage(message: any, type: any) {
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
            fetchFileStatus(true); // Silent refresh

            // Also refresh tasks if tasks section is open
            if (showTasksSection) {
              fetchTasks(true); // Silent refresh
            }
          }
        }
      }
    });
  }

  // Cleanup on component destroy
  onDestroy(() => {
    if (refreshInterval) {
      clearInterval(refreshInterval);
    }
    if (unsubscribeWebSocket) {
      unsubscribeWebSocket();
    }
    window.removeEventListener('cache-invalidated', handleCacheInvalidation);
    if (selectedFile !== null) unlockScroll();
  });
</script>

<div class="file-status-container">
  <div class="header">
    <h2>{$t('fileStatus.title')}</h2>
    <div class="controls">
      <span class="live-status-icon" data-tooltip="Live updates via WebSocket. Fallback poll every 2 minutes.">
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10"></circle>
          <line x1="12" y1="16" x2="12" y2="12"></line>
          <line x1="12" y1="8" x2="12.01" y2="8"></line>
        </svg>
      </span>

      {#if isAdmin}
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
    <div class="error-message">
      {error}
    </div>
  {/if}

  {#if loading && !fileStatus}
    <div class="skeleton-status">
      <div class="status-cards">
        {#each Array(5) as _}
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
      on:pageChange={(e) => { taskPage = e.detail; fetchTasks(true); }}
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
    padding: 1rem;
    color: var(--text-color);
    height: calc(100vh - var(--content-top, 60px));
    height: calc(100dvh - var(--content-top, 60px));
    overflow-y: auto;
  }

  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 2rem;
  }

  .header h2 {
    margin: 0;
    color: var(--text-color);
  }

  .controls {
    display: flex;
    gap: 1rem;
    align-items: center;
  }

  .recovery-btn, .flower-btn {
    padding: 0.6rem 1.2rem;
    background: var(--primary-color);
    color: white;
    border: none;
    border-radius: 10px;
    cursor: pointer;
    transition: all 0.2s ease;
    font-weight: 500;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.95rem;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .flower-btn {
    background: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
  }

  .recovery-btn:hover {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
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
    color: #fff;
    font-size: 0.6875rem;
    font-weight: 400;
    padding: 6px 10px;
    border-radius: 6px;
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
    gap: 0.75rem;
    margin: 0 auto 0.75rem auto;
    max-width: 700px;
  }

  .status-card {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 1rem;
    text-align: center;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    transition: all 0.2s ease;
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
    font-size: 1.5rem;
    font-weight: bold;
    color: var(--text-color);
    margin-bottom: 0.25rem;
  }

  .status-label {
    color: var(--text-light);
    font-size: 0.8rem;
    font-weight: 500;
  }

  .error-message {
    background: var(--error-background);
    color: var(--error-color);
    padding: 1rem;
    border-radius: 4px;
    margin-bottom: 1rem;
    border: 1px solid var(--error-border);
  }

  :global([data-theme='dark']) .error-message {
    background: rgba(239, 68, 68, 0.1);
    border-color: rgba(239, 68, 68, 0.3);
  }

  @media (max-width: 768px) {
    .header {
      flex-direction: column;
      align-items: flex-start;
      gap: 1rem;
    }

    .controls {
      width: 100%;
      flex-wrap: wrap;
      gap: 0.5rem;
    }

    .status-cards {
      grid-template-columns: repeat(3, 1fr);
      gap: 0.5rem;
    }

    .status-card {
      padding: 0.6rem;
    }

    .status-number {
      font-size: 1.25rem;
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
    gap: 1.5rem;
  }

  .skeleton-card {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .skeleton-table {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem 1.5rem;
  }


</style>
