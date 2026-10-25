<script context="module" lang="ts">
  export interface ConfirmRequest {
    title: string;
    message: string;
    callback: () => void;
  }
</script>

<script lang="ts">
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { taskStatusIcon } from '$lib/ui/menuIcons';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { onMount, createEventDispatcher } from 'svelte';
  import { toastStore } from '$stores/toast';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import RetrySettings from '$components/settings/RetrySettings.svelte';
  import RecoveryActionRow from '$components/settings/RecoveryActionRow.svelte';
  import SettingsCard from '$components/settings/transcription/SettingsCard.svelte';
  import Badge from '$components/ui/Badge.svelte';
  import '$components/settings/transcription/formLayout.css';

  const dispatch = createEventDispatcher<{ requestConfirm: ConfirmRequest }>();

  // The API reports each list as `{ count, items }`.
  interface HealthList<T> {
    count: number;
    items: T[];
  }
  interface StuckTask {
    id: number;
    task_type: string;
    status: string;
    created_at: string;
  }
  interface InconsistentFile {
    uuid: string;
    filename: string;
    status: string | null;
  }
  interface TaskHealth {
    stuck_tasks?: HealthList<StuckTask>;
    inconsistent_files?: HealthList<InconsistentFile>;
  }

  let taskHealthData: TaskHealth | null = null;
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

  $: stuckTasks = taskHealthData?.stuck_tasks?.items ?? [];
  $: inconsistentFiles = taskHealthData?.inconsistent_files?.items ?? [];
  $: stuckCount = taskHealthData?.stuck_tasks?.count ?? stuckTasks.length;
  $: inconsistentCount = taskHealthData?.inconsistent_files?.count ?? inconsistentFiles.length;

  type BadgeVariant = 'default' | 'success' | 'warning' | 'error' | 'info';
  function badgeVariant(status: string | null): BadgeVariant {
    switch ((status ?? '').toLowerCase()) {
      case 'completed':
      case 'success':
        return 'success';
      case 'running':
      case 'processing':
      case 'in_progress':
        return 'info';
      case 'pending':
        return 'warning';
      case 'failed':
      case 'error':
        return 'error';
      default:
        return 'default';
    }
  }

  // Narrow screens show a status glyph (named for screen readers) instead of the word.
  const isMobileView = typeof window !== 'undefined' && window.innerWidth < 768;

  function formatStatus(rawStatus: string | null): string {
    const status = rawStatus ?? '';
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

<div class="recovery-tab tx-form">
  <div class="section-header-row">
    <p class="section-description">{$t('settings.taskHealth.description')}</p>
    <button
      type="button"
      class="btn btn-secondary btn-refresh"
      on:click={refreshTaskHealth}
      disabled={taskHealthLoading}
    >
      <svg class="refresh-icon" class:spinning={taskHealthLoading} xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
      </svg>
      {taskHealthLoading ? $t('settings.taskHealth.loading') : $t('settings.taskHealth.refresh')}
    </button>
  </div>

  <div class="settings-form">
    <RetrySettings />

    {#if taskHealthLoading}
      <SettingsSkeleton variant="dashboard" label={$t('settings.taskHealth.loadingMessage')} />
    {:else if taskHealthData}
      <SettingsCard
        title={$t('settings.taskHealth.systemRecovery')}
        description={$t('settings.taskHealth.systemRecoveryDesc')}
      >
        <div class="recovery-actions">
        <RecoveryActionRow
          id="stuck"
          title={$t('settings.taskHealth.recoverStuck')}
          help={$t('settings.taskHealth.recoverStuckHelp')}
          buttonLabel={`${$t('settings.taskHealth.recoverStuckButton')} (${stuckCount})`}
          on:run={recoverStuckTasks}
        />
        <RecoveryActionRow
          id="inconsistent"
          title={$t('settings.taskHealth.fixInconsistent')}
          help={$t('settings.taskHealth.fixInconsistentHelp')}
          buttonLabel={`${$t('settings.taskHealth.fixInconsistentButton')} (${inconsistentCount})`}
          on:run={fixInconsistentFiles}
        />
        <RecoveryActionRow
          id="startup"
          title={$t('settings.taskHealth.startupRecovery')}
          help={$t('settings.taskHealth.startupRecoveryHelp')}
          buttonLabel={$t('settings.taskHealth.startupRecoveryButton')}
          on:run={startupRecovery}
        />
        <RecoveryActionRow
          id="all-users"
          title={$t('settings.taskHealth.recoverAllUsers')}
          help={$t('settings.taskHealth.recoverAllUsersHelp')}
          buttonLabel={$t('settings.taskHealth.recoverAllUsersButton')}
          on:run={recoverAllUserFiles}
        />
        </div>
      </SettingsCard>

      {#if stuckTasks.length > 0}
        <SettingsCard title={$t('settings.taskHealth.stuckTasks')}>
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
                {#each stuckTasks as task (task.id)}
                  <tr>
                    <td>{task.id}</td>
                    <td>{task.task_type}</td>
                    <td><Badge variant={badgeVariant(task.status)}>{#if isMobileView && taskStatusIcon(task.status)}<span class="status-glyph" role="img" aria-label={formatStatus(task.status)} title={formatStatus(task.status)}><MenuIcon name={taskStatusIcon(task.status) ?? 'status-queued'} size="xs" /></span>{:else}{formatStatus(task.status)}{/if}</Badge></td>
                    <td>{new Date(task.created_at).toLocaleString()}</td>
                    <td>
                      <button type="button" class="btn btn-secondary btn-small" on:click={() => retryTask(task.id)}>
                        {$t('settings.taskHealth.retry')}
                      </button>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        </SettingsCard>
      {/if}

      {#if inconsistentFiles.length > 0}
        <SettingsCard title={$t('settings.taskHealth.inconsistentFiles')}>
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
                {#each inconsistentFiles as file (file.uuid)}
                  <tr>
                    <td>{file.uuid}</td>
                    <td>{file.filename}</td>
                    <td><Badge variant={badgeVariant(file.status)}>{#if isMobileView && taskStatusIcon(file.status)}<span class="status-glyph" role="img" aria-label={formatStatus(file.status)} title={formatStatus(file.status)}><MenuIcon name={taskStatusIcon(file.status) ?? 'status-queued'} size="xs" /></span>{:else}{formatStatus(file.status)}{/if}</Badge></td>
                    <td>
                      <button type="button" class="btn btn-secondary btn-small" on:click={() => retryFile(file.uuid)}>
                        {$t('settings.taskHealth.retry')}
                      </button>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        </SettingsCard>
      {/if}
    {:else}
      <div class="placeholder-message">
        <p>{$t('settings.taskHealth.clickRefresh')}</p>
      </div>
    {/if}
  </div>
</div>

<style>
  .section-header-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    margin-bottom: 1rem;
    padding-inline-end: 2rem;
  }

  .section-header-row .section-description {
    margin: 0;
  }

  .table-container {
    overflow-x: auto;
    border: 1px solid var(--border-color);
    border-radius: 8px;
  }

  .data-table {
    width: 100%;
    border-collapse: collapse;
    margin: 0;
    font-size: var(--font-size-sm);
  }

  .data-table th {
    padding: 0.5rem 0.75rem;
    text-align: start;
    font-size: var(--font-size-2xs);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
  }

  .data-table td {
    padding: 0.625rem 0.75rem;
  }

  .placeholder-message {
    text-align: center;
    padding: 2rem;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  @media (max-width: 768px) {
    .section-header-row {
      flex-direction: column;
      align-items: stretch;
      padding-inline-end: 0;
      gap: 0.5rem;
    }

    .section-header-row .btn-refresh {
      justify-content: center;
      min-height: 44px;
    }

    .data-table th,
    .data-table td {
      padding: 0.4rem 0.5rem;
      font-size: var(--font-size-xs);
    }
  }

  .status-glyph {
    display: inline-flex;
    align-items: center;
  }
</style>
