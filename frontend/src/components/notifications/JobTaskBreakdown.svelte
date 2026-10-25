<script lang="ts">
  import { t } from '$stores/locale';
  import { formatCompactDuration } from '$lib/utils/formatting';
  import {
    JOB_STEP_KEYS,
    JOB_TASK_KEYS,
    JOB_TASK_STATE_KEYS,
    JOB_TIER_KEYS,
    labelFor,
  } from '$lib/i18n/keyMaps';
  import type { JobTier, TaskState } from '$lib/notifications/processingJob';
  import TaskStateGlyph from './TaskStateGlyph.svelte';

  /**
   * Per-tier timing for one processing card. Diarization is listed as a transcription
   * step, not as a task of its own.
   */
  export let tiers: JobTier[];
  export let id: string;

  function timing(state: TaskState, seconds: number | null): string {
    return state === 'done' && seconds !== null
      ? formatCompactDuration(seconds)
      : $t(JOB_TASK_STATE_KEYS[state]);
  }
</script>

<div class="job-breakdown" {id}>
  {#each tiers as tier (tier.key)}
    <section class="tier state-{tier.state}">
      <h5 class="line tier-head">
        <TaskStateGlyph state={tier.state} />
        <span class="label">{$t(JOB_TIER_KEYS[tier.key])}</span>
        <span class="time">{$t(JOB_TASK_STATE_KEYS[tier.state])}</span>
      </h5>
      {#if tier.tasks.length > 0}
        <ul class="task-list">
          {#each tier.tasks.filter((task) => task.key !== 'diarization') as task (task.key)}
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
                      <span class="label">{labelFor($t, JOB_STEP_KEYS, sub.key)}</span>
                      <span class="time">{timing(sub.state, sub.durationSeconds)}</span>
                    </li>
                  {/each}
                </ul>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    </section>
  {/each}
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

  .tier + .tier {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid var(--border-color);
  }

  .tier-head {
    margin: 0 0 4px;
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-semibold);
  }

  .task-list {
    padding-inline-start: 8px;
  }

  .task-row + .task-row {
    margin-top: 4px;
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
