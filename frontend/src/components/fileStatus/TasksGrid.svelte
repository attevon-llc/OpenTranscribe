<script lang="ts">
  import InfoIcon from '$components/ui/InfoIcon.svelte';
  import ProgressBar from '$components/ui/ProgressBar.svelte';
  import type { UserTask } from '$lib/types/fileStatus';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import SearchPagination from '$components/search/SearchPagination.svelte';
  import { taskProgressPercent } from '$lib/utils/formatting';
  import Badge from '$components/ui/Badge.svelte';
  import { badgeVariantForStatus } from '$lib/fileStatus/statusVariant';
  import Banner from '$components/ui/Banner.svelte';

  export let tasks: UserTask[] = [];
  export let filteredTasks: UserTask[] = [];
  export let tasksLoading = false;
  export let tasksError: string | null = null;
  export let taskFilter = 'all';
  export let taskTypeFilter = 'all';
  export let taskPage = 1;
  export let taskTotalPages = 0;

  const dispatch = createEventDispatcher<{ viewDetails: string; pageChange: number }>();
</script>

{#if tasksLoading && tasks.length === 0}
  <div class="loading">{$t('fileStatus.loadingTasks')}</div>
{:else if tasksError}
  <Banner tone="error">{tasksError}</Banner>
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
                <Badge variant={badgeVariantForStatus(task.status)} size="md" class="status-badge">
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
                </Badge>
                {#if task.status === 'in_progress'}
                  <!-- `taskProgressPercent` guards missing/null/out-of-range values.
                       This rendered `task.progress * 100` raw, so a task without a
                       progress field produced `style="width: NaN%"`. -->
                  {@const progressPercent = taskProgressPercent(task.progress)}
                  <div class="progress-bar-container">
                    <ProgressBar
                      percent={progressPercent}
                      ariaLabel={task.media_file?.filename || $t('fileStatus.inProgress')}
                    />
                  </div>
                  <span class="task-progress-text" dir="ltr">{progressPercent}%</span>
                {/if}
              </div>
            </td>
            <td class="task-actions-cell">
              {#if task.media_file}
                {@const mediaFileUuid = task.media_file.uuid}
                <button
                  class="info-button small"
                  on:click={() => dispatch('viewDetails', mediaFileUuid)}
                  title={$t('fileStatus.viewDetailsTooltip')}
                >
                  <InfoIcon />
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
    color: var(--text-secondary);
    padding: var(--space-1-5);
    border-radius: var(--radius-round);
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all var(--duration-base) ease;
    flex-shrink: 0;
  }

  .info-button:hover {
    background-color: var(--button-hover);
    color: var(--primary-on-surface);
    transform: scale(1.1);
  }

  .info-button.small {
    padding: var(--space-1);
  }

  .tasks-table-wrapper {
    overflow-x: auto;
    border-radius: var(--radius-md);
    border: 1px solid var(--border-color);
  }

  .tasks-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-md);
    margin-bottom: 0;
    box-shadow: none;
  }

  .tasks-table thead th {
    text-align: start;
    padding: var(--space-2-5) var(--space-3);
    font-weight: var(--font-weight-semibold);
    font-size: var(--font-size-xs);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
    background: var(--background-color);
    border-bottom: 1px solid var(--border-color);
    white-space: nowrap;
  }

  .tasks-table thead th.col-actions {
    width: 3rem;
    text-align: center;
  }

  .tasks-table tbody td {
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--border-color);
    vertical-align: middle;
    color: var(--text-color);
  }

  .tasks-table tbody tr:last-child td {
    border-bottom: none;
  }

  .tasks-table tbody tr:hover {
    background: var(--table-row-hover);
  }

  .task-type-cell {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-weight: var(--font-weight-medium);
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
    font-weight: var(--font-weight-medium);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .task-filename.muted {
    color: var(--text-secondary);
  }

  .task-error-inline {
    display: block;
    font-size: var(--font-size-xs);
    /* 3.76:1 as --error-color, even on plain white — the fill red is simply not a text
     * colour at 12px. */
    color: var(--color-error-text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 300px;
    margin-top: var(--space-0-5);
  }

  .task-status-cell {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    white-space: nowrap;
  }

  .task-status-cell .progress-bar-container {
    width: 60px;
  }

  .task-progress-text {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-semibold);
    color: var(--text-secondary);
  }

  .task-actions-cell {
    text-align: center;
    width: 3rem;
  }

  .no-tasks {
    text-align: center;
    padding: var(--space-8);
    color: var(--text-secondary);
  }

  .loading {
    text-align: center;
    padding: var(--space-8);
    color: var(--text-secondary);
  }

</style>
