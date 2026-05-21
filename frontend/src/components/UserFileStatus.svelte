<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import axiosInstance from '../lib/axios';
  import { apiCache, cacheKey, CacheTTL } from '$lib/apiCache';
  import { user } from '../stores/auth';
  import { websocketStore } from '../stores/websocket';
  import { toastStore } from '../stores/toast';
  import { t } from '../stores/locale';
  import { getFlowerUrl } from '$lib/utils/url';
  import SkeletonLoader from './ui/SkeletonLoader.svelte';
  import { DatePicker } from '@svelte-plugins/datepicker';
  import { format } from 'date-fns';
  import SearchPagination from './search/SearchPagination.svelte';

  // Helper function to translate status values
  function translateStatus(status: string): string {
    const statusMap: Record<string, string> = {
      'completed': $t('common.completed'),
      'processing': $t('common.processing'),
      'pending': $t('common.pending'),
      'error': $t('common.error'),
      'failed': $t('fileStatus.failed'),
      'in_progress': $t('fileStatus.inProgress'),
      'Completed': $t('common.completed'),
      'Processing': $t('common.processing'),
      'Pending': $t('common.pending'),
      'Error': $t('common.error'),
      'Failed': $t('fileStatus.failed'),
      'In Progress': $t('fileStatus.inProgress'),
    };
    return statusMap[status] || status;
  }

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

  // Date picker state
  let datePickerOpen = false;
  let dpStartDate: Date | string | null = null;
  let dpEndDate: Date | string | null = null;

  function handleDatePickerChange(event: { startDate: Date | string; endDate?: Date | string }) {
    const start = event.startDate ? new Date(event.startDate) : null;
    const end = event.endDate ? new Date(event.endDate) : null;
    if (start && !isNaN(start.getTime())) {
      taskDateFrom = format(start, 'yyyy-MM-dd');
    }
    if (end && !isNaN(end.getTime())) {
      taskDateTo = format(end, 'yyyy-MM-dd');
      datePickerOpen = false;
    }
  }

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
    } catch (err: any) {
      console.error('Error fetching file status:', err);
      if (!silent) {
        error = err.response?.data?.detail || $t('fileStatus.loadFailed');
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
    } catch (err: any) {
      console.error('Error fetching tasks:', err);
      if (!silent) {
        tasksError = err.response?.data?.detail || $t('fileStatus.tasksLoadFailed');
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
    window.open(url, '_blank');
  }

  async function fetchDetailedStatus(fileId: any) {
    try {
      const response = await axiosInstance.get(`/my-files/${fileId}/status`);
      detailedStatus = response.data;
      selectedFile = fileId;
      lockScroll();
    } catch (err: any) {
      console.error('Error fetching detailed status:', err);
      error = err.response?.data?.detail || $t('fileStatus.detailsLoadFailed');
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

    } catch (err: any) {
      console.error('Error retrying file:', err);
      const errorMsg = err.response?.data?.detail || $t('fileStatus.retryFailed');
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

    } catch (err: any) {
      console.error('Error requesting recovery:', err);
      const errorMsg = err.response?.data?.detail || $t('fileStatus.recoveryFailed');
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

  function formatDate(dateString: any) {
    if (!dateString) return 'N/A';

    const date = new Date(dateString);
    return new Intl.DateTimeFormat('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    }).format(date);
  }

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
    <!-- Quick filter chips -->
    <div class="quick-filters">
      <button class="quick-chip" class:active={taskFilter === 'all'} on:click={() => { taskFilter = 'all'; }}>{$t('fileStatus.allStatuses')}</button>
      <button class="quick-chip attention" class:active={taskFilter === 'needs_attention'} on:click={() => { taskFilter = 'needs_attention'; }}>
        {$t('fileStatus.filesNeedAttention')}
        {#if fileStatus?.has_problems}
          <span class="chip-badge">{fileStatus.problem_files.count}</span>
        {/if}
      </button>
      <button class="quick-chip" class:active={taskFilter === 'in_progress'} on:click={() => { taskFilter = 'in_progress'; }}>{$t('fileStatus.inProgress')}</button>
      <button class="quick-chip" class:active={taskFilter === 'pending'} on:click={() => { taskFilter = 'pending'; }}>{$t('common.pending')}</button>
      <button class="quick-chip" class:active={taskFilter === 'failed'} on:click={() => { taskFilter = 'failed'; }}>{$t('common.error')}</button>
      <button class="quick-chip" class:active={taskFilter === 'completed'} on:click={() => { taskFilter = 'completed'; }}>{$t('common.completed')}</button>
    </div>

    <div class="compact-filters">
        <select bind:value={taskTypeFilter} class="compact-filter-select">
          <option value="all">{$t('fileStatus.allTypes')}</option>
          <option value="transcription">{$t('fileStatus.transcription')}</option>
          <option value="summarization">{$t('fileStatus.summarization')}</option>
          <option value="search_indexing">{$t('search.settingsTitle')}</option>
        </select>

        <select bind:value={taskAgeFilter} class="compact-filter-select">
          <option value="all">{$t('fileStatus.allAges')}</option>
          <option value="today">{$t('fileStatus.last24h')}</option>
          <option value="week">{$t('fileStatus.lastWeek')}</option>
          <option value="month">{$t('fileStatus.lastMonth')}</option>
          <option value="older">{$t('fileStatus.older')}</option>
        </select>

        <div class="date-picker-inline">
          <DatePicker
            isRange
            enableFutureDates
            bind:isOpen={datePickerOpen}
            bind:startDate={dpStartDate}
            bind:endDate={dpEndDate}
            onDateChange={handleDatePickerChange}
          >
            <button
              type="button"
              class="date-trigger-btn"
              on:click={() => datePickerOpen = !datePickerOpen}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect>
                <line x1="16" y1="2" x2="16" y2="6"></line>
                <line x1="8" y1="2" x2="8" y2="6"></line>
                <line x1="3" y1="10" x2="21" y2="10"></line>
              </svg>
              <span class="date-text">
                {#if taskDateFrom && taskDateTo}
                  {taskDateFrom} — {taskDateTo}
                {:else if taskDateFrom}
                  {taskDateFrom} — ...
                {:else}
                  {$t('filter.selectDateRange')}
                {/if}
              </span>
            </button>
          </DatePicker>
        </div>

        {#if taskFilter !== 'all' || taskTypeFilter !== 'all' || taskAgeFilter !== 'all' || taskDateFrom || taskDateTo}
          <button
            class="compact-clear-btn"
            on:click={() => {
              taskFilter = 'all';
              taskTypeFilter = 'all';
              taskAgeFilter = 'all';
              taskDateFrom = '';
              taskDateTo = '';
              dpStartDate = null;
              dpEndDate = null;
              datePickerOpen = false;
            }}
            title={$t('fileStatus.clearFilters')}
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        {/if}
      </div>

      {#if tasksLoading && tasks.length === 0}
        <div class="loading">{$t('fileStatus.loadingTasks')}</div>
      {:else if tasksError}
        <div class="error-message">{tasksError}</div>
      {:else if filteredTasks.length === 0}
        <div class="no-tasks">
          <p>{taskFilter !== 'all' || taskTypeFilter !== 'all' ? $t('fileStatus.noTasksFilters') : $t('fileStatus.noTasks')}</p>
        </div>
      {:else}
        <div class="tasks-table-wrapper">
          <table class="tasks-table">
            <thead>
              <tr>
                <th>{$t('fileStatus.taskType')}</th>
                <th>{$t('fileStatus.fileName')}</th>
                <th>{$t('common.status')}</th>
                <th class="col-actions"></th>
              </tr>
            </thead>
            <tbody>
              {#each filteredTasks as task (task.id)}
                <tr class="task-row">
                  <td>
                    <div class="task-type-cell">
                      {#if task.task_type === 'transcription'}
                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                          <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path>
                          <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                          <line x1="12" y1="19" x2="12" y2="23"></line>
                          <line x1="8" y1="23" x2="16" y2="23"></line>
                        </svg>
                        {$t('fileStatus.transcription')}
                      {:else if task.task_type === 'search_indexing'}
                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                          <circle cx="11" cy="11" r="8"></circle>
                          <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                        </svg>
                        {$t('search.settingsTitle')}
                      {:else}
                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                          <line x1="8" y1="6" x2="21" y2="6"></line>
                          <line x1="8" y1="12" x2="21" y2="12"></line>
                          <line x1="8" y1="18" x2="21" y2="18"></line>
                          <line x1="3" y1="6" x2="3.01" y2="6"></line>
                        </svg>
                        {$t('fileStatus.summarization')}
                      {/if}
                    </div>
                  </td>
                  <td class="task-file-cell">
                    {#if task.media_file}
                      <span class="task-filename">{task.media_file.filename}</span>
                    {:else}
                      <span class="task-filename muted">—</span>
                    {/if}
                    {#if task.error_message}
                      <span class="task-error-inline" title={task.error_message}>{task.error_message}</span>
                    {/if}
                  </td>
                  <td>
                    <div class="task-status-cell">
                      <span class="status-badge {task.status === 'completed' ? 'status-completed' : task.status === 'in_progress' ? 'status-processing' : task.status === 'pending' ? 'status-pending' : task.status === 'failed' ? 'status-error' : 'status-unknown'}">
                        {#if task.status === 'pending'}
                          {$t('common.pending')}
                        {:else if task.status === 'in_progress'}
                          {$t('fileStatus.inProgress')}
                        {:else if task.status === 'completed'}
                          {$t('common.completed')}
                        {:else if task.status === 'failed'}
                          {$t('fileStatus.failed')}
                        {:else}
                          {task.status}
                        {/if}
                      </span>
                      {#if task.status === 'in_progress'}
                        <div class="progress-bar-container">
                          <div class="progress-bar" style="width: {task.progress * 100}%"></div>
                        </div>
                        <span class="task-progress-text">{Math.round(task.progress * 100)}%</span>
                      {/if}
                    </div>
                  </td>
                  <td class="task-actions-cell">
                    {#if task.media_file}
                      <button
                        class="info-button small"
                        on:click={() => fetchDetailedStatus(task.media_file.uuid)}
                        title={$t('fileStatus.viewDetailsTooltip')}
                      >
                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                          <circle cx="12" cy="12" r="10"></circle>
                          <line x1="12" y1="16" x2="12" y2="12"></line>
                          <line x1="12" y1="8" x2="12.01" y2="8"></line>
                        </svg>
                      </button>
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
          {#if taskTotalPages > 1}
            <SearchPagination
              page={taskPage}
              totalPages={taskTotalPages}
              on:pageChange={(e) => { taskPage = e.detail; fetchTasks(true); }}
            />
          {/if}
        </div>
      {/if}
    </div>

  {#if detailedStatus && selectedFile}
    <!-- svelte-ignore a11y-click-events-have-key-events -->
    <!-- svelte-ignore a11y-no-static-element-interactions -->
    <div
      class="detailed-status-modal"
      role="presentation"
      on:click={closeModal}
      on:wheel|preventDefault|self
      on:touchmove|preventDefault|self
      on:keydown={(e) => e.key === 'Escape' && closeModal()}
    >
      <!-- svelte-ignore a11y-click-events-have-key-events -->
      <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
      <!-- svelte-ignore a11y-no-static-element-interactions -->
      <!-- svelte-ignore a11y_interactive_supports_focus -->
      <div
        class="modal-content"
        role="dialog"
        aria-modal="true"
        on:click|stopPropagation
        on:keydown|stopPropagation
      >
        <div class="modal-header">
          <h3>{$t('fileStatus.fileDetails')}: {detailedStatus.file.filename}</h3>
          <button class="close-btn" on:click={closeModal}>×</button>
        </div>

        <div class="modal-body">
          <!-- File Details Grid -->
          <div class="file-details">
            <h4>{$t('fileStatus.fileInformation')}</h4>
            <div class="metadata-grid">
              <div class="metadata-item">
                <span class="metadata-label">{$t('fileStatus.fileName')}:</span>
                <span class="metadata-value">{detailedStatus.file.filename}</span>
              </div>
              <div class="metadata-item">
                <span class="metadata-label">{$t('common.status')}:</span>
                <span class="status-badge {detailedStatus.file.status_badge_class || 'status-unknown'}">
                  {translateStatus(detailedStatus.file.display_status || detailedStatus.file.status)}
                </span>
              </div>
              <div class="metadata-item">
                <span class="metadata-label">{$t('fileStatus.fileSize')}:</span>
                <span class="metadata-value">{detailedStatus.file.formatted_file_size || $t('fileStatus.unknown')}</span>
              </div>
              <div class="metadata-item">
                <span class="metadata-label">{$t('common.duration')}:</span>
                <span class="metadata-value">{detailedStatus.file.formatted_duration || $t('fileStatus.unknown')}</span>
              </div>
              <div class="metadata-item">
                <span class="metadata-label">{$t('fileStatus.language')}:</span>
                <span class="metadata-value">{detailedStatus.file.language || $t('fileStatus.autoDetected')}</span>
              </div>
              <div class="metadata-item">
                <span class="metadata-label">{$t('fileStatus.uploadTime')}:</span>
                <span class="metadata-value">{formatDate(detailedStatus.file.upload_time)}</span>
              </div>
              {#if detailedStatus.file.completed_at}
                <div class="metadata-item">
                  <span class="metadata-label">{$t('fileStatus.completedAt')}:</span>
                  <span class="metadata-value">{formatDate(detailedStatus.file.completed_at)}</span>
                </div>
              {/if}
              <div class="metadata-item">
                <span class="metadata-label">{$t('fileStatus.fileAge')}:</span>
                <span class="metadata-value">{detailedStatus.file.formatted_file_age || $t('fileStatus.unknown')}</span>
              </div>
              {#if detailedStatus.file.whisper_model}
                <div class="metadata-item">
                  <span class="metadata-label">{$t('fileDetail.whisperModel')}:</span>
                  <span class="metadata-value model-name-value">
                    {detailedStatus.file.whisper_model}
                    {#if detailedStatus.file.model_fallback_occurred}
                      <span class="fallback-badge" title="{$t('fileDetail.requestedModel')}: {detailedStatus.file.requested_whisper_model}">
                        {$t('fileDetail.modelFallback')}
                      </span>
                    {/if}
                  </span>
                </div>
              {/if}
              {#if detailedStatus.file.diarization_disabled}
                <div class="metadata-item">
                  <span class="metadata-label">Diarization:</span>
                  <span class="metadata-value diarization-disabled-value">{$t('metadata.diarizationDisabled')}</span>
                </div>
              {:else if detailedStatus.file.diarization_model}
                <div class="metadata-item">
                  <span class="metadata-label">Diarization:</span>
                  <span class="metadata-value model-name-value">{detailedStatus.file.diarization_model}</span>
                </div>
              {/if}
            </div>

            {#if detailedStatus.is_stuck}
              <div class="warning">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display: inline; margin-right: 4px;">
                  <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/>
                  <path d="M12 9v4"/>
                  <path d="m12 17 .01 0"/>
                </svg>
                {$t('fileStatus.fileStuck')}
              </div>
            {/if}

            {#if detailedStatus.can_retry}
              <div class="retry-section">
                <button
                  class="retry-btn large"
                  on:click={() => retryFile(selectedFile)}
                  disabled={retryingFiles.has(selectedFile)}
                >
                  {retryingFiles.has(selectedFile) ? $t('fileStatus.retrying') : $t('fileStatus.retryProcessing')}
                </button>
              </div>
            {/if}
          </div>

          {#if detailedStatus.task_details.length > 0}
            <div class="task-details">
              <h4>{$t('fileStatus.taskDetailsTitle')}</h4>
              <div class="task-metadata-grid">
                {#each detailedStatus.task_details as task}
                  <div class="task-metadata-card">
                    <div class="task-card-header">
                      <span class="task-type-label">{task.task_type}</span>
                      <span class="status-badge {task.status_badge_class || 'status-unknown'}">{translateStatus(task.status)}</span>
                    </div>
                    <div class="task-metadata-items">
                      <div class="metadata-item">
                        <span class="metadata-label">{$t('fileStatus.taskCreated')}</span>
                        <span class="metadata-value">{formatDate(task.created_at)}</span>
                      </div>
                      {#if task.updated_at}
                        <div class="metadata-item">
                          <span class="metadata-label">{$t('fileStatus.lastUpdated')}</span>
                          <span class="metadata-value">{formatDate(task.updated_at)}</span>
                        </div>
                      {/if}
                      {#if task.completed_at}
                        <div class="metadata-item">
                          <span class="metadata-label">{$t('fileStatus.taskCompleted')}</span>
                          <span class="metadata-value">{formatDate(task.completed_at)}</span>
                        </div>
                        <div class="metadata-item">
                          <span class="metadata-label">{$t('fileStatus.processingTime')}</span>
                          <span class="metadata-value">{task.formatted_processing_time || $t('common.unknown')}</span>
                        </div>
                      {/if}
                      {#if task.progress !== undefined && task.status === 'in_progress'}
                        <div class="metadata-item">
                          <span class="metadata-label">{$t('fileStatus.progress')}</span>
                          <span class="metadata-value">{Math.round(task.progress * 100)}%</span>
                        </div>
                      {/if}
                      {#if task.whisper_model}
                        <div class="metadata-item">
                          <span class="metadata-label">Whisper Model</span>
                          <span class="metadata-value model-name-value">{task.whisper_model}</span>
                        </div>
                      {/if}
                      {#if task.diarization_model}
                        <div class="metadata-item">
                          <span class="metadata-label">Diarization</span>
                          <span class="metadata-value model-name-value">{task.diarization_model}</span>
                        </div>
                      {/if}
                    </div>
                    {#if task.error_message}
                      <div class="task-error-details">
                        <span class="metadata-label">{$t('fileStatus.errorLabel')}</span>
                        <div class="task-error">{task.error_message}</div>
                      </div>
                    {/if}
                  </div>
                {/each}
              </div>
            </div>
          {/if}
        </div>
      </div>
    </div>
  {/if}
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

  .recovery-btn, .flower-btn, .tasks-toggle-btn {
    padding: 0.6rem 1.2rem;
    background: #3b82f6;
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

  .recovery-btn:hover, .tasks-toggle-btn:hover {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .recovery-btn:active, .tasks-toggle-btn:active {
    transform: scale(1);
  }

  .flower-btn:hover {
    background: var(--button-hover);
    border-color: var(--border-hover);
  }

  .recovery-btn:disabled, .tasks-toggle-btn:disabled {
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

  :global(.dark) .live-status-icon::after,
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

  :global(.dark) .status-card {
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
  }

  :global(.dark) .status-card:hover {
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

  .problems-section {
    background: var(--warning-background);
    border: 1px solid var(--warning-border);
    border-radius: 8px;
    padding: 1.5rem;
    margin-bottom: 2rem;
  }

  :global(.dark) .problems-section {
    background: rgba(245, 158, 11, 0.1);
    border-color: rgba(245, 158, 11, 0.3);
  }

  .problems-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
  }

  .problem-files {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .problem-file {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--background-color);
    padding: 1rem;
    border-radius: 6px;
    border: 1px solid var(--border-color);
    transition: all 0.2s ease;
  }

  .problem-file:hover {
    border-color: var(--border-hover);
    transform: scale(1.02);
  }

  .file-info {
    flex: 1;
  }

  .filename {
    font-weight: 500;
    margin-bottom: 0.25rem;
    color: var(--text-color);
  }

  .file-meta {
    display: flex;
    gap: 1rem;
    align-items: center;
  }

  .file-age {
    color: var(--text-light);
    font-size: 0.875rem;
  }

  .file-actions {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .details-btn, .retry-btn {
    padding: 0.25rem 0.75rem;
    font-size: 0.875rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.2s;
  }

  .details-btn {
    background: var(--background-color);
    color: var(--text-color);
    border-color: var(--border-color);
  }

  .details-btn:hover {
    background: var(--surface-color);
    border-color: var(--border-hover);
  }

  .retry-btn {
    background: var(--success-color);
    color: white;
    border-color: var(--success-color);
  }

  .retry-btn:hover {
    background: var(--success-hover);
    border-color: var(--success-hover);
    transform: scale(1.02);
  }

  .retry-btn:disabled {
    background: var(--text-light);
    border-color: var(--text-light);
    cursor: not-allowed;
    transform: none;
  }

  .status-badge {
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 500;
    text-transform: uppercase;
  }

  .status-completed {
    background: rgba(16, 185, 129, 0.1);
    color: #10b981;
  }

  .status-processing {
    background: rgba(59, 130, 246, 0.1);
    color: var(--primary-color);
  }

  .status-pending {
    background: rgba(245, 158, 11, 0.1);
    color: #f59e0b;
  }

  .status-error {
    background: rgba(239, 68, 68, 0.1);
    color: #ef4444;
  }

  .status-unknown {
    background: rgba(156, 163, 175, 0.1);
    color: #6b7280;
  }

  :global(.dark) .status-completed {
    background: rgba(16, 185, 129, 0.2);
    color: #34d399;
  }

  :global(.dark) .status-processing {
    background: rgba(59, 130, 246, 0.2);
    color: #60a5fa;
  }

  :global(.dark) .status-pending {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }

  :global(.dark) .status-error {
    background: rgba(239, 68, 68, 0.2);
    color: #f87171;
  }

  .no-problems {
    text-align: center;
    padding: 0.75rem;
    background: var(--success-background);
    border: 1px solid var(--success-border);
    border-radius: 8px;
    color: var(--success-color);
  }

  :global(.dark) .no-problems {
    background: rgba(16, 185, 129, 0.1);
    border-color: rgba(16, 185, 129, 0.3);
    color: #34d399;
  }

  .recent-files {
    margin-top: 1rem;
  }

  .recent-files-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
    gap: 0.75rem;
  }

  .recent-file-card {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 0.75rem;
    transition: all 0.2s ease;
  }

  .recent-file-card:hover {
    transform: scale(1.02);
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
    border-color: var(--border-hover);
  }

  .file-status-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
  }

  .status-info {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    flex: 1;
  }

  .info-button {
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-secondary-color);
    padding: 6px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s ease;
    flex-shrink: 0;
  }

  .info-button:hover {
    background-color: rgba(0, 0, 0, 0.05);
    color: var(--primary-color);
    transform: scale(1.1);
  }

  :global(.dark) .info-button:hover {
    background-color: rgba(255, 255, 255, 0.1);
  }

  .info-button.small {
    padding: 4px;
  }

  /* Quick filter chips */
  .quick-filters {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
    margin-bottom: 0.75rem;
  }

  .quick-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.375rem 0.75rem;
    height: 30px;
    box-sizing: border-box;
    font-size: 0.8125rem;
    font-weight: 500;
    border: none;
    border-radius: 10px;
    background: rgba(59, 130, 246, 0.08);
    color: #3b82f6;
    cursor: pointer;
    transition: all 0.15s ease;
    white-space: nowrap;
  }

  :global(.dark) .quick-chip,
  :global([data-theme='dark']) .quick-chip {
    background: rgba(96, 165, 250, 0.12);
    color: #93c5fd;
  }

  .quick-chip:hover {
    background: rgba(59, 130, 246, 0.15);
    transform: translateY(-1px);
  }

  :global(.dark) .quick-chip:hover,
  :global([data-theme='dark']) .quick-chip:hover {
    background: rgba(96, 165, 250, 0.2);
  }

  .quick-chip.active {
    background: #3b82f6;
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .quick-chip.active:hover {
    background: #2563eb;
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .quick-chip.attention .chip-badge {
    background: rgba(239, 68, 68, 0.15);
    color: #ef4444;
    font-size: 0.6875rem;
    padding: 1px 6px;
    border-radius: 10px;
    font-weight: 600;
  }

  .quick-chip.attention.active .chip-badge {
    background: rgba(255, 255, 255, 0.25);
    color: white;
  }

  /* Tasks Section Styles */
  .tasks-section {
    margin-top: 1.5rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem 1.5rem 1.5rem;
  }

  .tasks-header {
    margin-bottom: 1rem;
  }

  .compact-filters {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    padding: 0.75rem;
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    margin-bottom: 1.5rem;
    width: fit-content;
  }

  .compact-filter-select {
    padding: 0.375rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    cursor: pointer;
    width: auto;
    min-width: 0;
    height: 30px;
    transition: border-color 0.15s ease;
  }

  .compact-filter-select:hover {
    border-color: var(--primary-color);
  }

  .compact-filter-select:focus {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .date-picker-inline {
    position: relative;
  }

  .date-trigger-btn {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.375rem 0.625rem;
    height: 30px;
    box-sizing: border-box;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    font-family: inherit;
    cursor: pointer;
    transition: border-color 0.15s ease;
    white-space: nowrap;
  }

  .date-trigger-btn:hover {
    border-color: var(--primary-color);
  }

  .date-text {
    color: var(--text-secondary);
    font-size: 0.75rem;
  }

  .date-picker-inline :global(.datepicker) {
    font-family: inherit;
  }

  .date-picker-inline :global(.datepicker .calendars-container) {
    position: absolute !important;
    top: calc(100% + 4px);
    right: 0;
    z-index: 100;
    width: 280px !important;
    border-radius: 10px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
    --datepicker-container-background: var(--surface-color, #fff);
    --datepicker-container-border: 1px solid var(--border-color, #e8e9ea);
    --datepicker-container-border-radius: 10px;
    --datepicker-container-box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
    --datepicker-color: var(--text-color, #21333d);
    --datepicker-border-color: var(--border-color, #e8e9ea);
    --datepicker-state-active: var(--primary-color, #3b82f6);
    --datepicker-state-hover: var(--hover-color, #e7f7fc);
    --datepicker-font-size-base: 0.8rem;
    --datepicker-calendar-width: 100%;
    --datepicker-calendar-padding: 4px 4px 12px;
    --datepicker-calendar-day-height: 32px;
    --datepicker-calendar-day-width: 32px;
    --datepicker-calendar-day-font-size: 0.8rem;
    --datepicker-calendar-dow-font-size: 0.75rem;
    --datepicker-calendar-header-font-size: 0.95rem;
    --datepicker-calendar-day-color: var(--text-color, #232a32);
    --datepicker-calendar-day-background-hover: var(--hover-color, #f5f5f5);
    --datepicker-calendar-dow-color: var(--text-secondary, #8b9198);
    --datepicker-calendar-header-color: var(--text-color, #21333d);
    --datepicker-calendar-header-text-color: var(--text-color, #21333d);
    --datepicker-calendar-header-month-nav-color: var(--text-color, #21333d);
    --datepicker-calendar-header-month-nav-background-hover: var(--hover-color, #f5f5f5);
    --datepicker-calendar-today-border: 1px solid var(--text-color, #232a32);
    --datepicker-calendar-day-other-color: var(--text-secondary, #d1d3d6);
  }

  .date-picker-inline :global(.datepicker .calendars-container .calendar) {
    width: 100% !important;
    padding: 4px 4px 12px !important;
  }

  .date-picker-inline :global(.datepicker .calendars-container .calendar .date span) {
    width: 32px !important;
    height: 32px !important;
    font-size: 0.8rem !important;
  }

  :global(.dark) .date-picker-inline :global(.datepicker .calendars-container),
  :global([data-theme='dark']) .date-picker-inline :global(.datepicker .calendars-container) {
    --datepicker-container-background: var(--surface-color, #1e293b);
    --datepicker-container-box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
    --datepicker-color: var(--text-color, #e2e8f0);
    --datepicker-state-hover: rgba(59, 130, 246, 0.15);
    --datepicker-calendar-day-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-day-background-hover: rgba(255, 255, 255, 0.08);
    --datepicker-calendar-dow-color: var(--text-secondary, #94a3b8);
    --datepicker-calendar-header-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-header-text-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-header-month-nav-color: var(--text-color, #e2e8f0);
    --datepicker-calendar-header-month-nav-background-hover: rgba(255, 255, 255, 0.08);
    --datepicker-calendar-today-border: 1px solid var(--text-color, #e2e8f0);
    --datepicker-calendar-day-other-color: var(--text-secondary, #475569);
  }

  .compact-clear-btn {
    padding: 0.35rem;
    background: var(--error-color);
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
  }

  .compact-clear-btn:hover {
    background: var(--error-hover);
    transform: scale(1.1);
  }


  .tasks-table-wrapper {
    overflow-x: auto;
    border-radius: 6px;
    border: 1px solid var(--border-color);
  }

  .tasks-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.875rem;
    margin-bottom: 0;
    box-shadow: none;
  }

  .tasks-table thead th {
    text-align: left;
    padding: 0.6rem 0.75rem;
    font-weight: 600;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary-color);
    background: var(--background-color);
    border-bottom: 1px solid var(--border-color);
    white-space: nowrap;
  }

  .tasks-table thead th.col-actions {
    width: 3rem;
    text-align: center;
  }

  .tasks-table tbody td {
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border-color);
    vertical-align: middle;
    color: var(--text-color);
  }

  .tasks-table tbody tr:last-child td {
    border-bottom: none;
  }

  .tasks-table tbody tr:hover {
    background: var(--table-row-hover, rgba(0, 0, 0, 0.02));
  }

  :global(.dark) .tasks-table tbody tr:hover {
    background: rgba(255, 255, 255, 0.03);
  }

  .task-type-cell {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 500;
    white-space: nowrap;
  }

  .task-type-cell svg {
    flex-shrink: 0;
    opacity: 0.7;
  }

  .task-file-cell {
    max-width: 300px;
  }

  .task-filename {
    display: block;
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .task-filename.muted {
    color: var(--text-secondary-color);
  }

  .task-error-inline {
    display: block;
    font-size: 0.75rem;
    color: var(--error-color);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 300px;
    margin-top: 0.15rem;
  }

  .task-status-cell {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    white-space: nowrap;
  }

  .task-status-cell .progress-bar-container {
    width: 60px;
    height: 4px;
    background: var(--border-color);
    border-radius: 2px;
    overflow: hidden;
  }

  .task-status-cell .progress-bar {
    height: 100%;
    background: #3b82f6;
    border-radius: 2px;
    transition: width 0.3s ease;
  }

  .task-progress-text {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--text-secondary-color);
  }

  .task-actions-cell {
    text-align: center;
    width: 3rem;
  }

  .no-tasks {
    text-align: center;
    padding: 2rem;
    color: var(--text-secondary-color);
  }

  .detailed-status-modal {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    overflow: hidden;
    overscroll-behavior: none;
  }

  :global(.dark) .detailed-status-modal {
    background: rgba(0, 0, 0, 0.7);
  }

  .modal-content {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    max-width: 600px;
    width: 90%;
    max-height: 80vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
  }

  :global(.dark) .modal-content {
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.3), 0 10px 10px -5px rgba(0, 0, 0, 0.2);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.5rem;
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
  }

  .modal-header h3 {
    margin: 0;
    color: var(--text-color);
  }

  .close-btn {
    background: none;
    border: none;
    font-size: 1.5rem;
    cursor: pointer;
    color: var(--text-light);
    transition: color 0.2s ease;
  }

  .close-btn:hover {
    color: var(--text-color);
  }

  .modal-body {
    padding: 1.5rem;
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }

  .file-details h4 {
    margin: 0 0 1rem 0;
    color: var(--text-color);
    font-weight: 600;
  }

  .metadata-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 0.75rem;
    margin-bottom: 1.5rem;
  }

  .metadata-item {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .metadata-label {
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--text-secondary-color);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .metadata-value {
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text-color);
    word-break: break-word;
  }

  .model-name-value {
    font-family: 'SF Mono', 'Fira Code', 'Fira Mono', 'Roboto Mono', monospace;
    font-size: 0.85rem;
  }

  .diarization-disabled-value {
    font-style: italic;
    color: var(--text-secondary);
  }

  .fallback-badge {
    display: inline-block;
    margin-left: 0.4rem;
    padding: 0.1rem 0.4rem;
    font-size: 0.7rem;
    font-family: inherit;
    font-weight: 500;
    border-radius: 4px;
    background-color: rgba(var(--warning-color-rgb, 217, 119, 6), 0.15);
    color: var(--warning-color, #d97706);
    border: 1px solid rgba(var(--warning-color-rgb, 217, 119, 6), 0.3);
    vertical-align: middle;
    cursor: help;
  }

  .task-metadata-grid {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .task-metadata-card {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 1rem;
  }

  .task-card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .task-type-label {
    font-weight: 600;
    color: var(--text-color);
    text-transform: capitalize;
  }

  .task-metadata-items {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 0.75rem;
  }

  .task-error-details {
    margin-top: 1rem;
    padding: 0.75rem;
    background: rgba(var(--error-color-rgb, 239, 68, 68), 0.05);
    border-radius: 4px;
    border: 1px solid rgba(var(--error-color-rgb, 239, 68, 68), 0.2);
  }

  .task-error-details .metadata-label {
    color: var(--error-color);
  }

  .task-error-details .task-error {
    margin-top: 0.5rem;
    font-family: monospace;
    white-space: pre-wrap;
    font-size: 0.85rem;
    color: var(--error-color);
  }

  .detail-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--border-color);
  }

  .label {
    font-weight: 500;
    color: var(--text-color);
  }

  .warning {
    background: var(--warning-background);
    color: var(--warning-text);
    padding: 0.75rem;
    border-radius: 4px;
    margin: 1rem 0;
    border: 1px solid var(--warning-border);
  }

  :global(.dark) .warning {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
    border-color: rgba(245, 158, 11, 0.3);
  }

  .suggestions {
    margin: 1rem 0;
  }

  .retry-section {
    text-align: center;
    margin: 1rem 0;
  }

  .retry-btn.large {
    padding: 0.75rem 1.5rem;
    font-size: 1rem;
  }

  .task-details {
    margin-top: 1.5rem;
    border-top: 1px solid var(--border-color);
    padding-top: 1.5rem;
  }

  .task-details h4 {
    color: var(--text-color);
    margin: 0 0 1rem 0;
  }

  .tasks-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .task-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.5rem;
    background: var(--surface-color);
    border-radius: 4px;
    border: 1px solid var(--border-color);
  }

  .task-type {
    color: var(--text-color);
    font-weight: 500;
  }

  .task-error {
    color: var(--error-color);
    font-size: 0.875rem;
    margin-top: 0.25rem;
  }

  .loading {
    text-align: center;
    padding: 2rem;
    color: var(--text-light);
  }

  .error-message {
    background: var(--error-background);
    color: var(--error-color);
    padding: 1rem;
    border-radius: 4px;
    margin-bottom: 1rem;
    border: 1px solid var(--error-border);
  }

  :global(.dark) .error-message {
    background: rgba(239, 68, 68, 0.1);
    border-color: rgba(239, 68, 68, 0.3);
  }

  /* Collapsible section styles */
  .recent-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .section-toggle-btn {
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-secondary-color);
    padding: 4px;
    border-radius: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s ease;
  }

  .section-toggle-btn:hover {
    background: var(--surface-color);
    color: var(--text-color);
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

    .compact-filters {
      padding: 0.6rem;
      gap: 0.4rem;
      width: 100%;
    }

    .compact-filter-select {
      font-size: 0.75rem;
      padding: 0.3rem 0.4rem;
    }

    .compact-date-input {
      width: 100px;
      font-size: 0.75rem;
      padding: 0.3rem 0.4rem;
    }

    .compact-clear-btn {
      width: 24px;
      height: 24px;
      padding: 0.3rem;
    }

    .problem-file {
      flex-direction: column;
      align-items: flex-start;
      gap: 1rem;
    }

    .file-actions {
      align-self: stretch;
      justify-content: flex-end;
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
