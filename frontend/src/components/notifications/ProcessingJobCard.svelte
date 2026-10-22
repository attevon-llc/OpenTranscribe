<script context="module" lang="ts">
  let uid = 0;
</script>

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { JOB_STATE_KEYS, JOB_TASK_KEYS, JOB_TASK_STATE_KEYS } from '$lib/i18n/keyMaps';
  import type { ProcessingJobModel } from '$lib/notifications/processingJob';
  import { fileLinkFor, formatRelativeTime, type Tone } from '$lib/notifications/display';
  import NotificationShell from './NotificationShell.svelte';
  import NotificationProgress from './NotificationProgress.svelte';
  import TaskStateGlyph from './TaskStateGlyph.svelte';
  import JobTaskBreakdown from './JobTaskBreakdown.svelte';

  /** One file's processing: overall bar, a chip per task, and an expandable timing list. */
  export let job: ProcessingJobModel;
  export let now: number;

  uid += 1;
  const detailsId = `job-details-${uid}`;
  let expanded = false;

  const dispatch = createEventDispatcher<{
    open: { ids: string[]; link: string };
    dismiss: { ids: string[] };
  }>();

  const TONE: Record<ProcessingJobModel['state'], Tone> = {
    running: 'info',
    done: 'success',
    failed: 'error',
  };

  $: link = fileLinkFor(job.fileId) ?? '';
  // The full name, truncated by CSS: pre-shortening to "name...ext" and then letting the
  // one-line ellipsis clip that again rendered "fair......" at panel width.
  $: name = job.filename || $t('common.viewFile');
  $: detail = job.state === 'failed' ? job.failure?.message : job.currentStep;
</script>

<NotificationShell
  icon="logo"
  tone={TONE[job.state]}
  read={job.read}
  processing={job.state === 'running'}
  dismissible={job.dismissible}
  extraClass="processing-job"
  on:dismiss={() => dispatch('dismiss', { ids: job.memberIds })}
>
  <h4 class="notification-title">
    <a
      href={link}
      class="job-file"
      dir="auto"
      title={job.filename || undefined}
      on:click|preventDefault={() => dispatch('open', { ids: job.memberIds, link })}
    >{name}</a>
  </h4>
  <p class="job-status">
    <span class="job-state">{$t(JOB_STATE_KEYS[job.state])}</span>
    {#if detail}
      <span class="job-detail" dir="auto">{detail}</span>
    {/if}
  </p>

  <NotificationProgress
    percent={job.percent}
    label={$t('notifications.job.progressLabel', { filename: job.filename || name })}
    active={job.state === 'running'}
    failed={job.state === 'failed'}
    eta={job.etaDisplay ?? ''}
  />

  <ul class="job-chips">
    {#each job.tasks as task (task.key)}
      <li class="job-chip state-{task.state}">
        <TaskStateGlyph state={task.state} />
        <span aria-hidden="true">{$t(JOB_TASK_KEYS[task.key])}</span>
        <span class="visually-hidden">
          {$t('notifications.job.chipLabel', {
            task: $t(JOB_TASK_KEYS[task.key]),
            state: $t(JOB_TASK_STATE_KEYS[task.state]),
          })}
        </span>
      </li>
    {/each}
  </ul>

  <div class="job-footer">
    <span class="notification-time">{formatRelativeTime(job.timestamp, now, $t)}</span>
    <button
      type="button"
      class="job-toggle"
      aria-expanded={expanded}
      aria-controls={detailsId}
      on:click={() => (expanded = !expanded)}
    >
      <svg class="chevron" class:open={expanded} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="6 9 12 15 18 9"></polyline>
      </svg>
      {expanded ? $t('notifications.job.hideDetails') : $t('notifications.job.showDetails')}
    </button>
  </div>

  {#if expanded}
    <JobTaskBreakdown id={detailsId} tasks={job.tasks} />
  {/if}
</NotificationShell>

<style>
  .notification-title {
    margin: 0 0 2px 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    line-height: var(--line-height-tight);
    min-width: 0;
  }

  .job-file {
    display: block;
    color: var(--primary-on-surface);
    text-decoration: none;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .job-file:hover {
    text-decoration: underline;
  }

  .job-status {
    display: flex;
    flex-wrap: wrap;
    gap: 0 6px;
    margin: 0;
    font-size: var(--font-size-sm);
    line-height: var(--line-height-snug);
    color: var(--text-secondary);
  }

  .job-state {
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  .job-detail {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .job-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin: 8px 0 0;
    padding: 0;
    list-style: none;
  }

  .job-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-medium);
    line-height: 1.4;
    white-space: nowrap;
    border-radius: 10px;
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    background: var(--surface-color);
  }

  .job-chip.state-done {
    color: var(--success-color);
    background: var(--success-bg);
    border-color: transparent;
  }

  .job-chip.state-running {
    color: var(--primary-on-surface);
    background: rgba(var(--primary-color-rgb), 0.1);
    border-color: transparent;
  }

  .job-chip.state-failed {
    color: var(--error-color);
    background: var(--error-background);
    border-color: transparent;
  }

  .job-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-top: 8px;
  }

  .notification-time {
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  .job-toggle {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 4px;
    border: none;
    border-radius: 4px;
    background: transparent;
    /* A text disclosure, not a raised button: cancel form-elements.css's shadow and hover. */
    box-shadow: none;
    color: var(--primary-on-surface);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    cursor: pointer;
  }

  .job-toggle:hover {
    text-decoration: underline;
    background: transparent;
    box-shadow: none;
    transform: none;
  }

  .job-toggle:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }

  .chevron {
    transition: transform 0.15s ease;
  }

  .chevron.open {
    transform: rotate(180deg);
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  @media (prefers-reduced-motion: reduce) {
    .chevron {
      transition: none;
    }
  }
</style>
