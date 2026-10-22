<script lang="ts">
  import { t } from '$stores/locale';
  import { formatCompactDuration } from '$lib/utils/formatting';
  import {
    ENRICHMENT_TASK_KEYS,
    JOB_STEP_KEYS,
    JOB_TASK_KEYS,
    JOB_TASK_STATE_KEYS,
    labelFor,
  } from '$lib/i18n/keyMaps';
  import type { JobSubtask, JobTask, TaskState } from '$lib/notifications/processingJob';
  import TaskStateGlyph from './TaskStateGlyph.svelte';

  /** Per-task timing for one processing card; diarization is listed as a transcription step. */
  export let tasks: JobTask[];
  export let id: string;

  $: rows = tasks.filter((task) => task.key !== 'diarization');

  function subLabel(task: JobTask, sub: JobSubtask): string {
    return task.key === 'enrichment'
      ? labelFor($t, ENRICHMENT_TASK_KEYS, sub.key)
      : labelFor($t, JOB_STEP_KEYS, sub.key);
  }

  function timing(state: TaskState, seconds: number | null): string {
    return state === 'done' && seconds !== null
      ? formatCompactDuration(seconds)
      : $t(JOB_TASK_STATE_KEYS[state]);
  }
</script>

<div class="job-breakdown" {id}>
  <ul class="task-list">
    {#each rows as task (task.key)}
      <li class="task-row state-{task.state}">
        <div class="line">
          <TaskStateGlyph state={task.state} />
          <span class="label">{$t(JOB_TASK_KEYS[task.key])}</span>
          <span class="time">{timing(task.state, task.durationSeconds)}</span>
        </div>
        {#if task.state === 'failed' && task.message}
          <p class="task-message" dir="auto">{task.message}</p>
        {/if}
        {#if task.subtasks.length > 0}
          <ul class="sub-list">
            {#each task.subtasks as sub (sub.key)}
              <li class="line sub state-{sub.state}">
                <TaskStateGlyph state={sub.state} />
                <span class="label">{subLabel(task, sub)}</span>
                <span class="time">{timing(sub.state, sub.durationSeconds)}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </li>
    {/each}
  </ul>
</div>

<style>
  .job-breakdown {
    margin-top: 8px;
    padding: 8px 10px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
  }

  .task-list,
  .sub-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .task-row + .task-row {
    margin-top: 6px;
    padding-top: 6px;
    border-top: 1px solid var(--border-color);
  }

  .sub-list {
    margin-top: 2px;
    padding-inline-start: 20px;
  }

  .line {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: var(--font-size-xs);
    line-height: var(--line-height-snug);
    color: var(--text-color);
  }

  .line.sub {
    color: var(--text-secondary);
  }

  .label {
    flex: 1;
    min-width: 0;
    font-weight: var(--font-weight-medium);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .sub .label {
    font-weight: var(--font-weight-regular);
  }

  .time {
    flex-shrink: 0;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .state-failed > .line .time,
  .line.state-failed .time {
    color: var(--error-color);
  }

  .task-message {
    margin: 2px 0 0;
    padding-inline-start: 20px;
    font-size: var(--font-size-xs);
    color: var(--error-color);
    overflow-wrap: anywhere;
  }
</style>
