/**
 * Consolidated processing notification: one card per file instead of one row per event type.
 *
 * Pure view model. The store keeps one record per event type per file (the file page, gallery
 * and status page dispatch on `notification.type`); `jobGrouping.ts` groups a file's records
 * and this module derives the card in three tiers:
 *
 *   main       import, transcription (+ its backend-reported stages), diarization
 *   finishing  search indexing, facts, analytics, redaction, speaker attributes /
 *              suggestions / clustering
 *   ai         summary, its index, topics, applying tag/collection suggestions
 *
 * The finishing and AI tiers come from the backend's post-processing run
 * (`postProcessing.ts`): a plan names every task that will run (or was skipped by a
 * setting), so the card is "Complete" only when every known task has finished, says
 * "Finishing up" while any is outstanding, and says it cannot confirm the final steps when
 * no plan was ever seen, rather than guessing. Design and the event inventory:
 * `docs/design/consolidated_processing_notification_plan.md`.
 */
import type { Notification } from '$stores/websocket';
import { formatCompactDuration } from '$lib/utils/formatting';
import { JOB_TASK_BY_TYPE, type JobItem, type MemberTask } from './jobGrouping';
import { taskStateFromStatus, type StepMark, type TaskState } from './runTiming';
import type { PostProcessingEntry, PostProcessingRun } from './postProcessing';

export type { TaskState } from './runTiming';

export type TierKey = 'main' | 'finishing' | 'ai';

/**
 * The post-processing task keys, the backend's `PostProcessingTask` wire values, each filed
 * under its tier. `backend/tests/unit/test_post_processing_status.py` fails if this map and
 * the enum diverge, so a new backend task cannot render nowhere.
 */
export const POST_PROCESSING_TIERS = {
  speaker_attributes: 'finishing',
  speaker_identification: 'finishing',
  speaker_clustering: 'finishing',
  search_indexing: 'finishing',
  file_facts: 'finishing',
  analytics: 'finishing',
  redaction: 'finishing',
  summary: 'ai',
  topics: 'ai',
  summary_indexing: 'ai',
  auto_label: 'ai',
} as const satisfies Record<string, Exclude<TierKey, 'main'>>;

export type PostProcessingKey = keyof typeof POST_PROCESSING_TIERS;
export type JobTaskKey = 'import' | 'transcription' | 'diarization' | PostProcessingKey;

const POST_KEYS = Object.keys(POST_PROCESSING_TIERS) as PostProcessingKey[];

/** Share of the overall bar. Diarization is reported inside the transcription percent. */
export const TASK_WEIGHTS: Record<JobTaskKey, number> = {
  import: 10,
  transcription: 60,
  diarization: 0,
  speaker_attributes: 3,
  speaker_identification: 2,
  speaker_clustering: 1,
  search_indexing: 4,
  file_facts: 2,
  analytics: 1,
  redaction: 2,
  summary: 10,
  topics: 4,
  summary_indexing: 1,
  auto_label: 2,
};

/** Held by the finishing tier before the plan arrives, so the bar cannot reach 100%. */
const FINISHING_PLACEHOLDER_WEIGHT = 15;

/**
 * How long after the transcript is done the card waits for the post-processing plan before
 * saying it cannot confirm the final steps (the plan normally lands within a second).
 */
export const PLAN_GRACE_MS = 2 * 60_000;
/** An outstanding run with no frame for this long is flagged as possibly stale. */
export const STALE_AFTER_MS = 10 * 60_000;
/** When the card should ask the server for the run (a missed frame, a reload). */
export const RECONCILE_AFTER_MS = 30_000;
/** The server keeps a run this long (`POST_PROCESSING_STATUS_TTL_S`); older is unknowable. */
export const RUN_RETENTION_MS = 2 * 24 * 3600_000;

/**
 * Transcription sub-stages in display order. The keys are the backend's `TranscriptionStage`
 * wire values, sent as `data.stage` on every processing frame;
 * `backend/tests/unit/test_transcription_stage_wire.py` fails if the two lists diverge.
 */
export const TRANSCRIPTION_STAGES = [
  'prepare',
  'transcribe',
  'diarize',
  'finalize',
  'speakers',
] as const;
export type TranscriptionStageKey = (typeof TRANSCRIPTION_STAGES)[number];

const DIARIZE_INDEX = TRANSCRIPTION_STAGES.indexOf('diarize');
/** A task that is running but reports no number is shown half done. */
const INDETERMINATE_PERCENT = 50;

export interface JobSubtask {
  key: string;
  state: TaskState;
  durationSeconds: number | null;
}

export interface JobTask {
  key: JobTaskKey;
  tier: TierKey;
  state: TaskState;
  percent: number;
  weight: number;
  durationSeconds: number | null;
  message?: string;
  subtasks: JobSubtask[];
}

export interface JobTier {
  key: TierKey;
  state: TaskState;
  tasks: JobTask[];
  done: number;
  total: number;
}

/**
 * processing  the transcript is not ready yet
 * finishing   the transcript is ready, known post-processing is still outstanding
 * complete    every task the backend said would run has finished
 * attention   something failed (and nothing else is still running)
 * ready       the transcript is ready, but the final steps cannot be confirmed
 */
export type JobState = 'processing' | 'finishing' | 'complete' | 'attention' | 'ready';

export type FinalJobState = Extract<JobState, 'complete' | 'attention' | 'ready'>;

/** The outcomes a card reaches: announced once, and the end of its live updates. */
export function isFinalJobState(state: JobState): state is FinalJobState {
  return state === 'complete' || state === 'attention' || state === 'ready';
}

export interface ProcessingJobModel {
  key: string;
  fileId: string;
  filename: string;
  state: JobState;
  percent: number;
  tiers: JobTier[];
  /** Every visible task, in tier order. */
  tasks: JobTask[];
  /** Post-processing tasks still queued or running, in display order. */
  outstanding: JobTaskKey[];
  /** The plan has not arrived yet (finishing tier is preparing). */
  awaitingPlan: boolean;
  /** Outstanding work with no frame for {@link STALE_AFTER_MS}. */
  stale: boolean;
  /** The card should read the server's post-processing state for this file. */
  reconcile: boolean;
  memberIds: string[];
  read: boolean;
  dismissible: boolean;
  timestamp: Date;
  currentStep?: string;
  etaDisplay?: string;
  failure?: { task: JobTaskKey; message: string };
}

export interface JobModelOptions {
  llmAvailable: boolean;
  now: number;
}

function ms(d: Date | string | number | undefined): number {
  return d instanceof Date ? d.getTime() : new Date(d ?? Number.NaN).getTime();
}

function wireStatus(n: Notification): string | undefined {
  const s = n.data?.status ?? n.status;
  return typeof s === 'string' ? s : undefined;
}

function seconds(fromMs: number | undefined, toMs: number | undefined): number | null {
  if (fromMs === undefined || toMs === undefined || toMs < fromMs) return null;
  return (toMs - fromMs) / 1000;
}

/** Server-measured duration when the frame carries one, else what this client observed. */
function taskDuration(n: Notification): number | null {
  const server = n.data?.duration_seconds;
  if (typeof server === 'number' && server >= 0) return server;
  return seconds(n.startedAt, n.finishedAt);
}

function percentOf(n: Notification): number | null {
  const p = n.progress?.percentage ?? n.data?.progress;
  return typeof p === 'number' && Number.isFinite(p) ? p : null;
}

function clamp(p: number): number {
  return Math.min(100, Math.max(0, p));
}

function messageOf(n: Notification): string | undefined {
  const m = n.currentStep || n.message || (n.data?.message as string | undefined);
  return m || undefined;
}

function stageIndexOf(stage: string | undefined): number {
  return TRANSCRIPTION_STAGES.indexOf(stage as TranscriptionStageKey);
}

function tierOf(key: JobTaskKey): TierKey {
  return key in POST_PROCESSING_TIERS ? POST_PROCESSING_TIERS[key as PostProcessingKey] : 'main';
}

function isOutstanding(state: TaskState): boolean {
  return state === 'pending' || state === 'running';
}

/**
 * Sub-stage steps from the stage the backend sent on each frame (recorded on the step log by
 * `runTiming`). The current stage is the latest one seen: a frame without a stage leaves it
 * where it was, and a record with none at all is at its start. Time is attributed to a stage
 * from each mark until the next mark of a different stage, or until the run finished, and
 * summed, so a stage the backend revisits is still timed correctly.
 */
function transcriptionStages(tx: Notification, txState: TaskState): JobSubtask[] {
  const log: StepMark[] = (tx.stepLog ?? []).filter((m) => stageIndexOf(m.stage) >= 0);
  const current = log.length ? stageIndexOf(log[log.length - 1].stage) : 0;

  const spent = new Map<number, number>();
  log.forEach((mark, k) => {
    // Only the first mark of a contiguous block opens it; later ones are inside it.
    if (k > 0 && log[k - 1].stage === mark.stage) return;
    const next = log.slice(k + 1).find((m) => m.stage !== mark.stage);
    const d = seconds(mark.at, next ? next.at : tx.finishedAt);
    if (d === null) return;
    const i = stageIndexOf(mark.stage);
    spent.set(i, (spent.get(i) ?? 0) + d);
  });

  return TRANSCRIPTION_STAGES.map((key, i) => {
    let state: TaskState;
    if (txState === 'done') state = 'done';
    else if (txState === 'failed')
      state = i < current ? 'done' : i === current ? 'failed' : 'skipped';
    else state = i < current ? 'done' : i === current ? 'running' : 'pending';
    const durationSeconds = state === 'done' ? spent.get(i) ?? null : null;
    return { key, state, durationSeconds };
  });
}

function makeTask(
  key: JobTaskKey,
  state: TaskState,
  over: Partial<Pick<JobTask, 'percent' | 'durationSeconds' | 'message' | 'subtasks'>> = {}
): JobTask {
  return {
    key,
    tier: tierOf(key),
    state,
    percent: state === 'done' ? 100 : 0,
    weight: TASK_WEIGHTS[key],
    durationSeconds: null,
    subtasks: [],
    ...over,
  };
}

function recordTask(key: JobTaskKey, n: Notification, runningPercent: number | null): JobTask {
  const state = taskStateFromStatus(wireStatus(n));
  // A failed task keeps the progress it reached (0 when it never reported one): counting
  // it as finished drew a full "100%" bar under a red failure card.
  const percent =
    state === 'done'
      ? 100
      : state === 'running'
        ? clamp(runningPercent ?? INDETERMINATE_PERCENT)
        : state === 'failed'
          ? clamp(runningPercent ?? 0)
          : 0;
  return makeTask(key, state, {
    percent,
    durationSeconds: state === 'done' ? taskDuration(n) : null,
    message: state === 'running' || state === 'failed' ? messageOf(n) : undefined,
  });
}

const ENTRY_STATE: Record<PostProcessingEntry['state'], TaskState> = {
  queued: 'pending',
  started: 'running',
  done: 'done',
  failed: 'failed',
  skipped: 'skipped',
};

/** A task from the post-processing run, with detail from its own record when it has one. */
function entryTask(
  key: PostProcessingKey,
  entry: PostProcessingEntry,
  rec?: Notification
): JobTask {
  const state = ENTRY_STATE[entry.state];
  const recordPercent = rec ? percentOf(rec) : null;
  const serverDuration = rec?.data?.duration_seconds;
  const durationSeconds =
    state !== 'done'
      ? null
      : typeof serverDuration === 'number' && serverDuration >= 0
        ? serverDuration
        : entry.startedAt !== undefined && entry.at >= entry.startedAt
          ? entry.at - entry.startedAt
          : null;
  return makeTask(key, state, {
    percent:
      state === 'done'
        ? 100
        : state === 'running'
          ? clamp(
              key === 'summary' && recordPercent !== null ? recordPercent : INDETERMINATE_PERCENT
            )
          : 0,
    durationSeconds,
    message: rec && (state === 'running' || state === 'failed') ? messageOf(rec) : undefined,
  });
}

function tierState(tasks: JobTask[]): TaskState {
  if (tasks.length === 0) return 'skipped';
  if (tasks.some((t) => t.state === 'running')) return 'running';
  const pending = tasks.some((t) => t.state === 'pending');
  if (pending)
    return tasks.some((t) => t.state === 'done' || t.state === 'failed') ? 'running' : 'pending';
  if (tasks.some((t) => t.state === 'failed')) return 'failed';
  return 'done';
}

const RECORD_FOR_KEY: Partial<Record<PostProcessingKey, MemberTask>> = {
  summary: 'summary',
  topics: 'topics',
  redaction: 'redaction',
};

/** Builds one file's card from its records. Never mutates them. */
export function buildJobModel(item: JobItem, opts: JobModelOptions): ProcessingJobModel {
  // The list is newest-first, so the first record of a task is its latest.
  const byTask = new Map<MemberTask, Notification>();
  for (const m of item.members) {
    const task = JOB_TASK_BY_TYPE[m.type];
    if (task && !byTask.has(task)) byTask.set(task, m);
  }

  const tx = byTask.get('transcription');
  const imp = byTask.get('import');
  const run: PostProcessingRun | undefined = byTask.get('postProcessing')?.postProcessing;
  const txStart = tx?.startedAt;
  // A downstream record older than the current transcription run is from a previous run.
  const current = (task: MemberTask) => {
    const rec = byTask.get(task);
    if (!rec || (txStart !== undefined && ms(rec.timestamp) < txStart)) return undefined;
    return rec;
  };

  // ---- main tier ------------------------------------------------------------------------
  const main: JobTask[] = [];
  let mainState: TaskState | undefined;
  if (imp) {
    const t = recordTask('import', imp, percentOf(imp));
    mainState = t.state;
    main.push(t);
  }
  if (tx) {
    const t = recordTask('transcription', tx, percentOf(tx));
    t.subtasks = transcriptionStages(tx, t.state);
    main.push(t);
    const diarize = t.subtasks[DIARIZE_INDEX];
    main.push(makeTask('diarization', diarize.state, { durationSeconds: diarize.durationSeconds }));
    mainState = t.state === 'done' && mainState === 'failed' ? 'failed' : t.state;
  } else if (imp && mainState !== 'failed') {
    main.push(makeTask('transcription', 'pending'));
    mainState = mainState === 'done' ? 'pending' : mainState;
  }
  const hasMain = main.length > 0;
  const mainDone = mainState === 'done';
  const mainFailed = mainState === 'failed';

  // ---- post-processing tiers --------------------------------------------------------------
  const planned = run !== undefined && run.runAt !== null;
  // A record saved before run timing existed has no finishedAt; its last frame is the end.
  const txFinishedAt = tx && mainDone ? tx.finishedAt ?? ms(tx.timestamp) : undefined;
  const graceOver = txFinishedAt !== undefined && opts.now - txFinishedAt > PLAN_GRACE_MS;
  const planUnknown = !planned && (run?.unknown === true || graceOver);
  const awaitingPlan = hasMain && mainDone && !planned && !planUnknown;

  const post: JobTask[] = [];
  for (const key of POST_KEYS) {
    const entry = run?.tasks[key];
    const recordType = RECORD_FOR_KEY[key];
    const rec = recordType ? current(recordType) : undefined;
    if (entry) {
      post.push(entryTask(key, entry, rec));
    } else if (rec) {
      // A record with no run entry (an older backend, or a manual regenerate on a file
      // whose card has no run) still shows what it says.
      post.push(recordTask(key, rec, key === 'summary' ? percentOf(rec) : null));
    } else if (
      !planned &&
      !planUnknown &&
      hasMain &&
      !mainFailed &&
      opts.llmAvailable &&
      (key === 'summary' || key === 'topics')
    ) {
      // Before the plan, an LLM deployment expects the AI tier; the plan then decides.
      post.push(makeTask(key, 'pending'));
    }
  }

  const visible = [...main, ...post].filter((t) => t.state !== 'skipped');
  const finishingTasks = visible.filter((t) => t.tier === 'finishing');
  const aiTasks = visible.filter((t) => t.tier === 'ai');

  const tiers: JobTier[] = [];
  if (hasMain) {
    tiers.push({
      key: 'main',
      state: mainState ?? 'pending',
      tasks: main.filter((t) => t.state !== 'skipped'),
      done: mainDone ? 1 : 0,
      total: 1,
    });
  }
  const finishingVisible = finishingTasks.length > 0 || (hasMain && !mainFailed && !planUnknown);
  if (finishingVisible) {
    let state = tierState(finishingTasks);
    if (finishingTasks.length === 0)
      state = awaitingPlan ? 'running' : planned ? 'done' : 'pending';
    else if (awaitingPlan && state === 'done') state = 'running';
    tiers.push({
      key: 'finishing',
      state,
      tasks: finishingTasks,
      done: finishingTasks.filter((t) => t.state === 'done').length,
      total: finishingTasks.length,
    });
  }
  if (aiTasks.length > 0) {
    tiers.push({
      key: 'ai',
      state: tierState(aiTasks),
      tasks: aiTasks,
      done: aiTasks.filter((t) => t.state === 'done').length,
      total: aiTasks.length,
    });
  }

  // ---- overall state ------------------------------------------------------------------------
  const outstandingTasks = visible.filter((t) => t.tier !== 'main' && isOutstanding(t.state));
  const failedTask = visible.find((t) => t.state === 'failed');
  let state: JobState;
  if (mainFailed) state = 'attention';
  else if (hasMain && !mainDone) state = 'processing';
  else if (outstandingTasks.length > 0 || awaitingPlan)
    state = hasMain ? 'finishing' : 'processing';
  else if (failedTask) state = 'attention';
  else if (hasMain && !planned) state = 'ready';
  else state = 'complete';

  // ---- percent ----------------------------------------------------------------------------------
  const counted = visible.filter((t) => t.weight > 0);
  let totalWeight = counted.reduce((sum, t) => sum + t.weight, 0);
  const weighted = counted.reduce(
    (sum, t) =>
      sum +
      t.weight *
        (t.state === 'done' ? 100 : isOutstanding(t.state) || t.state === 'failed' ? t.percent : 0),
    0
  );
  if (hasMain && !mainFailed && !planned && !planUnknown && finishingTasks.length === 0) {
    // Nothing known yet: a share of the bar stays empty until the plan names the tasks.
    totalWeight += FINISHING_PLACEHOLDER_WEIGHT;
  }
  let percent = totalWeight > 0 ? Math.round(weighted / totalWeight) : 0;
  if (state === 'complete' || state === 'ready') percent = 100;
  else if (state !== 'attention') percent = Math.min(99, percent);

  // ---- staleness and reconcile ---------------------------------------------------------------
  const lastFrame = run?.updatedAt;
  const stale =
    state === 'finishing' &&
    !awaitingPlan &&
    lastFrame !== undefined &&
    opts.now - lastFrame > STALE_AFTER_MS;
  const sinceTxDone = txFinishedAt !== undefined ? opts.now - txFinishedAt : 0;
  const reconcile =
    hasMain &&
    mainDone &&
    run?.unknown !== true &&
    sinceTxDone < RUN_RETENTION_MS &&
    ((awaitingPlan && sinceTxDone > RECONCILE_AFTER_MS) ||
      (state === 'ready' && !planned) ||
      (state === 'finishing' &&
        lastFrame !== undefined &&
        opts.now - lastFrame > STALE_AFTER_MS / 2));

  const runningMain = main.find((t) => t.state === 'running' && t.message);
  const newest = item.members[0];
  const filename =
    (item.members.map((m) => m.data?.filename).find((f) => typeof f === 'string' && f) as
      | string
      | undefined) ?? '';
  const eta =
    tx &&
    tx.progress &&
    taskStateFromStatus(wireStatus(tx)) === 'running' &&
    typeof tx.progress.etaSeconds === 'number' &&
    tx.progress.etaSeconds > 0
      ? formatCompactDuration(tx.progress.etaSeconds)
      : undefined;
  const failureTask = state === 'attention' ? failedTask : undefined;

  return {
    key: item.key,
    fileId: item.fileId,
    filename,
    state,
    percent,
    tiers,
    tasks: visible,
    outstanding: outstandingTasks.map((t) => t.key),
    awaitingPlan,
    stale,
    reconcile,
    memberIds: item.members.map((m) => m.id),
    read: item.members.every((m) => m.read),
    dismissible: item.members.every((m) => m.dismissible === true),
    timestamp: newest.timestamp,
    currentStep: state === 'processing' ? runningMain?.message : undefined,
    etaDisplay: state === 'processing' ? eta : undefined,
    failure: failureTask
      ? { task: failureTask.key, message: failureTask.message ?? '' }
      : undefined,
  };
}
