<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '../../stores/locale';
  import SearchPagination from '../search/SearchPagination.svelte';
  import { taskProgressPercent } from '$lib/utils/formatting';

  export let tasks: any[] = [];
  export let filteredTasks: any[] = [];
  export let tasksLoading = false;
  export let tasksError: any = null;
  export let taskFilter = 'all';
  export let taskTypeFilter = 'all';
  export let taskPage = 1;
  export let taskTotalPages = 0;

  const dispatch = createEventDispatcher<{ viewDetails: any; pageChange: number }>();
</script>

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
                  <!-- `taskProgressPercent` guards missing/null/out-of-range values.
                       This rendered `task.progress * 100` raw, so a task without a
                       progress field produced `style="width: NaN%"`. -->
                  {@const progressPercent = taskProgressPercent(task.progress)}
                  <div class="progress-bar-container">
                    <div class="progress-bar" style="width: {progressPercent}%"></div>
                  </div>
                  <span class="task-progress-text">{progressPercent}%</span>
                {/if}
              </div>
            </td>
            <td class="task-actions-cell">
              {#if task.media_file}
                <button
                  class="info-button small"
                  on:click={() => dispatch('viewDetails', task.media_file.uuid)}
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
        on:pageChange={(e) => dispatch('pageChange', e.detail)}
      />
    {/if}
  </div>
{/if}

<style>
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
</style>
