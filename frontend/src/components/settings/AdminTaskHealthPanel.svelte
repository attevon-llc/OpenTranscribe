<script context="module" lang="ts">
  export interface ConfirmRequest {
    title: string;
    message: string;
    callback: () => void;
  }
</script>

<script lang="ts">
  import { onMount, createEventDispatcher } from 'svelte';
  import { toastStore } from '$stores/toast';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import Spinner from '$components/ui/Spinner.svelte';
  import RetrySettings from '$components/settings/RetrySettings.svelte';

  const dispatch = createEventDispatcher<{ requestConfirm: ConfirmRequest }>();

  let taskHealthData: any = null;
  let taskHealthLoading = false;

  onMount(() => {
    loadTaskHealth();
  });

  async function loadTaskHealth() {
    taskHealthLoading = true;

    try {
      const response = await axiosInstance.get('/tasks/system/health');
      taskHealthData = response.data;
    } catch (err: unknown) {
      console.error('Error loading task health:', err);
      const message = getErrorMessage(err, $t('settings.toast.taskHealthLoadFailed'));
      toastStore.error(message);
    } finally {
      taskHealthLoading = false;
    }
  }

  async function refreshTaskHealth() {
    await loadTaskHealth();
  }

  function showConfirmation(title: string, message: string, callback: () => void) {
    dispatch('requestConfirm', { title, message, callback });
  }

  async function recoverStuckTasks() {
    showConfirmation(
      $t('settings.taskHealth.recoverStuck'),
      $t('settings.taskHealth.confirmRecoverStuck'),
      async () => {
        try {
          await axiosInstance.post('/tasks/recover-stuck-tasks');
          toastStore.success($t('settings.toast.stuckTasksRecoveryInitiated'));
          await refreshTaskHealth();
        } catch (err: unknown) {
          console.error('Error recovering stuck tasks:', err);
          const message = getErrorMessage(err, $t('settings.toast.stuckTasksRecoveryFailed'));
          toastStore.error(message);
        }
      }
    );
  }

  async function fixInconsistentFiles() {
    showConfirmation(
      $t('settings.taskHealth.fixInconsistent'),
      $t('settings.taskHealth.confirmFixInconsistent'),
      async () => {
        try {
          await axiosInstance.post('/tasks/fix-inconsistent-files');
          toastStore.success($t('settings.toast.inconsistentFilesFixInitiated'));
          await refreshTaskHealth();
        } catch (err: unknown) {
          console.error('Error fixing inconsistent files:', err);
          const message = getErrorMessage(err, $t('settings.toast.inconsistentFilesFixFailed'));
          toastStore.error(message);
        }
      }
    );
  }

  async function startupRecovery() {
    showConfirmation(
      $t('settings.taskHealth.startupRecovery'),
      $t('settings.taskHealth.confirmStartupRecovery'),
      async () => {
        try {
          await axiosInstance.post('/tasks/system/startup-recovery');
          toastStore.success($t('settings.toast.startupRecoveryInitiated'));
          await refreshTaskHealth();
        } catch (err: unknown) {
          console.error('Error running startup recovery:', err);
          const message = getErrorMessage(err, $t('settings.toast.startupRecoveryFailed'));
          toastStore.error(message);
        }
      }
    );
  }

  async function recoverAllUserFiles() {
    showConfirmation(
      $t('settings.taskHealth.recoverAllUsers'),
      $t('settings.taskHealth.confirmRecoverAllUsers'),
      async () => {
        try {
          await axiosInstance.post('/tasks/system/recover-all-user-files');
          toastStore.success($t('settings.toast.allUserFilesRecoveryInitiated'));
          await refreshTaskHealth();
        } catch (err: unknown) {
          console.error('Error recovering all user files:', err);
          const message = getErrorMessage(err, $t('settings.toast.allUserFilesRecoveryFailed'));
          toastStore.error(message);
        }
      }
    );
  }

  async function retryTask(taskId: number) {
    try {
      await axiosInstance.post(`/tasks/system/recover-task/${taskId}`);
      toastStore.success($t('settings.toast.taskRetryInitiated'));
      await refreshTaskHealth();
    } catch (err: unknown) {
      console.error('Error retrying task:', err);
      const message = getErrorMessage(err, $t('settings.toast.taskRetryFailed'));
      toastStore.error(message);
    }
  }

  async function retryFile(fileId: string) {
    try {
      await axiosInstance.post(`/tasks/retry/${fileId}`);
      toastStore.success($t('settings.toast.fileRetryInitiated'));
      await refreshTaskHealth();
    } catch (err: unknown) {
      console.error('Error retrying file:', err);
      const message = getErrorMessage(err, $t('settings.toast.fileRetryFailed'));
      toastStore.error(message);
    }
  }

  // Helper function for formatting status text
  // Uses compact symbols on small screens
  const isMobileView = typeof window !== 'undefined' && window.innerWidth < 768;

  function formatStatus(status: string): string {
    if (isMobileView) {
      const compactMap: Record<string, string> = {
        'completed': '✓',
        'success': '✓',
        'processing': '...',
        'in_progress': '...',
        'pending': '--',
        'error': '✗',
        'failed': '✗',
      };
      return compactMap[status.toLowerCase()] || status.slice(0, 4);
    }
    const statusMap: Record<string, string> = {
      'completed': $t('common.completed'),
      'processing': $t('common.processing'),
      'pending': $t('common.pending'),
      'error': $t('common.error'),
      'failed': $t('fileStatus.failed'),
      'in_progress': $t('fileStatus.inProgress'),
      'success': $t('common.success'),
    };
    return statusMap[status.toLowerCase()] || status.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }
</script>

<div class="content-section">
  <div class="section-header-row">
    <div>
      <h3 class="section-title">{$t('settings.taskHealth.title')}</h3>
      <p class="section-description">{$t('settings.taskHealth.description')}</p>
    </div>
    <button
      type="button"
      class="btn btn-secondary btn-refresh"
      on:click={refreshTaskHealth}
      disabled={taskHealthLoading}
    >
      <svg class="refresh-icon" class:spinning={taskHealthLoading} xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
      </svg>
      {taskHealthLoading ? $t('settings.taskHealth.loading') : $t('settings.taskHealth.refresh')}
    </button>
  </div>

  <!-- Retry Settings (task retry configuration) -->
  <div class="settings-subsection">
    <RetrySettings />
  </div>

  {#if taskHealthLoading}
    <div class="loading-state">
      <Spinner size="large" />
      <p>{$t('settings.taskHealth.loadingMessage')}</p>
    </div>
  {:else if taskHealthData}
    <div class="task-health-grid">
      <!-- Recovery Actions -->
      <div class="health-card">
        <h4>{$t('settings.taskHealth.systemRecovery')}</h4>
        <div class="action-buttons">
          <button class="btn btn-warning" on:click={recoverStuckTasks}>
            {$t('settings.taskHealth.recoverStuck')} ({taskHealthData.stuck_tasks?.length || 0})
          </button>
          <button class="btn btn-warning" on:click={fixInconsistentFiles}>
            {$t('settings.taskHealth.fixInconsistent')} ({taskHealthData.inconsistent_files?.length || 0})
          </button>
          <button class="btn btn-primary" on:click={startupRecovery}>
            {$t('settings.taskHealth.startupRecovery')}
          </button>
          <button class="btn btn-primary" on:click={recoverAllUserFiles}>
            {$t('settings.taskHealth.recoverAllUsers')}
          </button>
        </div>
      </div>

      <!-- Stuck Tasks -->
      {#if taskHealthData.stuck_tasks && taskHealthData.stuck_tasks.length > 0}
        <div class="health-card">
          <h4>{$t('settings.taskHealth.stuckTasks')}</h4>
          <div class="table-container">
            <table class="data-table">
              <thead>
                <tr>
                  <th>{$t('settings.taskHealth.id')}</th>
                  <th>{$t('settings.statistics.type')}</th>
                  <th>{$t('settings.statistics.status')}</th>
                  <th>{$t('settings.statistics.created')}</th>
                  <th>{$t('settings.taskHealth.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {#each taskHealthData.stuck_tasks as task}
                  <tr>
                    <td>{task.id}</td>
                    <td>{task.task_type}</td>
                    <td><span class="status-badge status-{task.status}">{formatStatus(task.status)}</span></td>
                    <td>{new Date(task.created_at).toLocaleString()}</td>
                    <td>
                      <button class="btn-small btn-primary" on:click={() => retryTask(task.id)}>
                        {$t('settings.taskHealth.retry')}
                      </button>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        </div>
      {/if}

      <!-- Inconsistent Files -->
      {#if taskHealthData.inconsistent_files && taskHealthData.inconsistent_files.length > 0}
        <div class="health-card">
          <h4>{$t('settings.taskHealth.inconsistentFiles')}</h4>
          <div class="table-container">
            <table class="data-table">
              <thead>
                <tr>
                  <th>{$t('settings.taskHealth.id')}</th>
                  <th>{$t('settings.taskHealth.filename')}</th>
                  <th>{$t('settings.statistics.status')}</th>
                  <th>{$t('settings.taskHealth.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {#each taskHealthData.inconsistent_files as file}
                  <tr>
                    <td>{file.uuid}</td>
                    <td>{file.filename}</td>
                    <td><span class="status-badge status-{file.status}">{formatStatus(file.status)}</span></td>
                    <td>
                      <button class="btn-small btn-primary" on:click={() => retryFile(file.uuid)}>
                        {$t('settings.taskHealth.retry')}
                      </button>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        </div>
      {/if}
    </div>
  {:else}
    <div class="placeholder-message">
      <p>{$t('settings.taskHealth.clickRefresh')}</p>
    </div>
  {/if}
</div>

<style>
  .section-title {
    font-size: 1.125rem;
    font-weight: 600;
    margin: 0 0 0.25rem 0;
    color: var(--text-color);
  }

  .section-description {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    margin: 0 0 1.25rem 0;
  }

  .btn {
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    border: none;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-primary {
    background-color: var(--primary-color);
    color: white;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-primary:hover:not(:disabled) {
    background-color: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-primary:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-secondary {
    background-color: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
  }

  .btn-secondary:hover:not(:disabled) {
    background-color: var(--button-hover);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
  }

  .btn-secondary:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-secondary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-warning {
    background-color: var(--warning-color);
    color: white;
    box-shadow: 0 2px 4px rgba(245, 158, 11, 0.2);
  }

  .btn-warning:hover:not(:disabled) {
    background-color: #d97706;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(245, 158, 11, 0.25);
  }

  .btn-warning:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-small {
    padding: 0.25rem 0.625rem;
    font-size: 0.75rem;
  }

  .btn-refresh {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
  }

  .refresh-icon {
    flex-shrink: 0;
    transition: transform 0.3s ease;
  }

  .refresh-icon.spinning {
    animation: spin 1s linear infinite;
  }

  .section-header-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    margin-bottom: 1rem;
    padding-right: 2rem;
  }

  .section-header-row .section-description {
    margin-bottom: 0;
  }

  .loading-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 2rem;
    color: var(--text-secondary);
  }

  .loading-state p {
    margin: 0;
    font-size: 0.8125rem;
  }

  .table-container {
    overflow-x: auto;
    border: 1px solid var(--border-color);
    border-radius: 8px;
  }

  .data-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .data-table thead {
    background-color: var(--background-color);
  }

  .data-table th {
    padding: 0.5rem 0.75rem;
    text-align: left;
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-color);
  }

  .data-table td {
    padding: 0.625rem 0.75rem;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .data-table tbody tr:last-child td {
    border-bottom: none;
  }

  .data-table tbody tr:hover {
    background-color: var(--background-color);
  }

  .status-badge {
    display: inline-block;
    padding: 0.125rem 0.5rem;
    border-radius: 10px;
    font-size: 0.6875rem;
    font-weight: 500;
    text-transform: capitalize;
  }

  .status-completed,
  .status-success {
    background-color: #d1fae5;
    color: #065f46;
  }

  .status-running,
  .status-processing,
  .status-in_progress {
    background-color: #dbeafe;
    color: #1e40af;
  }

  .status-pending {
    background-color: #fef3c7;
    color: #92400e;
  }

  .status-failed,
  .status-error {
    background-color: #fee2e2;
    color: #991b1b;
  }

  :global([data-theme='dark']) .status-completed,
  :global([data-theme='dark']) .status-success {
    background-color: #064e3b;
    color: #6ee7b7;
  }

  :global([data-theme='dark']) .status-running,
  :global([data-theme='dark']) .status-processing,
  :global([data-theme='dark']) .status-in_progress {
    background-color: #1e3a8a;
    color: #93c5fd;
  }

  :global([data-theme='dark']) .status-pending {
    background-color: #78350f;
    color: #fde68a;
  }

  :global([data-theme='dark']) .status-failed,
  :global([data-theme='dark']) .status-error {
    background-color: #7f1d1d;
    color: #fca5a5;
  }

  .task-health-grid {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .health-card {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem;
  }

  .health-card h4 {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-color);
    margin: 0 0 0.75rem 0;
  }

  .action-buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .placeholder-message {
    text-align: center;
    padding: 2rem;
    color: var(--text-secondary);
    font-size: 0.8125rem;
  }

  .settings-subsection {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1.25rem;
    margin-bottom: 1rem;
  }

  /* Responsive Design */
  @media (max-width: 768px) {
    .section-header-row {
      flex-direction: column;
      align-items: flex-start;
      padding-right: 0;
      gap: 0.5rem;
    }

    .section-header-row .btn-refresh {
      width: 100%;
      justify-content: center;
      min-height: 44px;
    }

    .action-buttons {
      flex-direction: column;
    }

    .action-buttons .btn {
      width: 100%;
      min-height: 44px;
      justify-content: center;
    }

    .task-health-grid .health-card {
      padding: 0.75rem;
    }

    .status-badge {
      padding: 0.1rem 0.35rem;
      font-size: 0.75rem;
      min-width: 24px;
      text-align: center;
    }

    .data-table th,
    .data-table td {
      padding: 0.4rem 0.5rem;
      font-size: 0.75rem;
    }

    .settings-subsection {
      padding: 0.75rem;
    }
  }
</style>
