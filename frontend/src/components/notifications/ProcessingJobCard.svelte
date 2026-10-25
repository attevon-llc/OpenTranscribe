<script context="module" lang="ts">
  let uid = 0;
</script>

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import {
    JOB_STATE_KEYS,
    JOB_TASK_KEYS,
    JOB_TASK_STATE_KEYS,
    JOB_TIER_KEYS,
  } from '$lib/i18n/keyMaps';
  import type {
    JobState,
    JobTask,
    JobTaskKey,
    JobTier,
    ProcessingJobModel,
  } from '$lib/notifications/processingJob';
  import { fileLinkFor, formatRelativeTime, type Tone } from '$lib/notifications/display';
  import NotificationShell from './NotificationShell.svelte';
  import NotificationProgress from './NotificationProgress.svelte';
  import TaskStateGlyph from './TaskStateGlyph.svelte';
  import JobTaskBreakdown from './JobTaskBreakdown.svelte';

  /**
   * One file's processing: overall bar, one chip per tier (main / finishing / AI), the
   * tasks still running or failed, and an expandable per-tier timing list.
   */
  export let job: ProcessingJobModel;
  export let now: number;

  /** Names listed in "Waiting on: ..." before the rest collapse into "+N". */
  const MAX_LISTED = 3;

  uid += 1;
  const detailsId = `job-details-${uid}`;
  let expanded = false;

  const dispatch = createEventDispatcher<{
    open: { ids: string[]; link: string };
    dismiss: { ids: string[] };
  }>();

  const TONE: Record<JobState, Tone> = {
    processing: 'info',
    finishing: 'info',
    complete: 'success',
    attention: 'error',
    ready: 'success',
  };

  $: link = fileLinkFor(job.fileId) ?? '';
  // The full name, truncated by CSS: pre-shortening to "name...ext" and then letting the
  // one-line ellipsis clip that again rendered "fair......" at panel width.
  $: name = job.filename || $t('common.viewFile');
  $: live = job.state === 'processing' || job.state === 'finishing';
  // Re-made whenever the translation function changes, i.e. on a language switch; the locale
  // store keeps <html lang> in step with it.
  $: listFormat = listFormatFor($t);
  $: detail = describe(job, $t, listFormat);
  $: activeTasks = activeTasksOf(job);

  function listFormatFor(_translate: typeof $t): Intl.ListFormat {
    const lang = typeof document === 'undefined' ? undefined : document.documentElement.lang;
    return new Intl.ListFormat(lang || undefined, { style: 'narrow', type: 'conjunction' });
  }

  function taskName(key: JobTaskKey, translate: typeof $t): string {
    return translate(JOB_TASK_KEYS[key]);
  }

  function describe(
    j: ProcessingJobModel,
    translate: typeof $t,
    list: Intl.ListFormat
  ): string | undefined {
    if (j.state === 'processing') return j.currentStep;
    if (j.state === 'finishing') {
      if (j.awaitingPlan) return translate('notifications.job.preparing');
      if (j.outstanding.length === 0) return undefined;
      const names = j.outstanding.slice(0, MAX_LISTED).map((k) => taskName(k, translate));
      const more = j.outstanding.length - names.length;
      const tasks = list.format(more > 0 ? [...names, `+${more}`] : names);
      return translate('notifications.job.waitingOn', { tasks });
    }
    if (j.state === 'attention' && j.failure) {
      const failed = translate('notifications.job.failedTask', {
        task: taskName(j.failure.task, translate),
      });
      return j.failure.message ? `${failed}: ${j.failure.message}` : failed;
    }
    if (j.state === 'ready') return translate('notifications.job.notConfirmed');
    return undefined;
  }

  /** What is happening now: running and failed tasks, else the waiting ones of a live job. */
  function activeTasksOf(j: ProcessingJobModel): JobTask[] {
    const busy = j.tasks.filter((task) => task.state === 'running' || task.state === 'failed');
    if (busy.length > 0 || j.state !== 'finishing') return busy;
    return j.tasks.filter((task) => task.tier !== 'main' && task.state === 'pending');
  }

  function tierLabel(tier: JobTier, translate: typeof $t): string {
    return translate('notifications.job.tierLabel', {
      tier: translate(JOB_TIER_KEYS[tier.key]),
      state: translate(JOB_TASK_STATE_KEYS[tier.state]),
      done: tier.done,
      total: tier.total,
    });
  }
</script>

<NotificationShell
  icon="logo"
  tone={TONE[job.state]}
  read={job.read}
  processing={live}
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
    <span class="job-state state-{job.state}">{$t(JOB_STATE_KEYS[job.state])}</span>
    {#if detail}
      <span class="job-detail" dir="auto">{detail}</span>
    {/if}
    {#if job.stale}
      <span class="job-stale">{$t('notifications.job.stale')}</span>
    {/if}
  </p>

  <NotificationProgress
    percent={job.percent}
    label={$t('notifications.job.progressLabel', { filename: job.filename || name })}
    active={live}
    failed={job.state === 'attention'}
    eta={job.etaDisplay ?? ''}
  />

  <ul class="job-chips tier-chips">
    {#each job.tiers as tier (tier.key)}
      <li class="job-chip state-{tier.state}">
        <TaskStateGlyph state={tier.state} />
        <span aria-hidden="true">{$t(JOB_TIER_KEYS[tier.key])}</span>
        {#if tier.key !== 'main' && tier.total > 1 && tier.state !== 'done'}
          <span class="tier-count" aria-hidden="true" dir="ltr">{tier.done}/{tier.total}</span>
        {/if}
        <span class="visually-hidden">{tierLabel(tier, $t)}</span>
      </li>
    {/each}
  </ul>

  {#if live || job.state === 'attention'}
    {#if activeTasks.length > 0}
      <ul class="job-chips task-chips">
        {#each activeTasks as task (task.key)}
          <li class="job-chip task-chip state-{task.state}">
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
    {/if}
  {/if}

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
    <JobTaskBreakdown id={detailsId} tiers={job.tiers} />
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

  .job-stale {
    font-style: italic;
  }

  .job-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin: 8px 0 0;
    padding: 0;
    list-style: none;
  }

  .task-chips {
    margin-top: 4px;
  }

  .task-chip {
    font-weight: var(--font-weight-regular);
  }

  .tier-count {
    font-variant-numeric: tabular-nums;
    opacity: 0.85;
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
