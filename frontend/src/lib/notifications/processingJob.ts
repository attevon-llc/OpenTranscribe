/**
 * Consolidated processing notification: one card per file instead of one row per event type.
 *
 * Pure view model. The store keeps one record per event type per file (the file page, gallery
 * and status page dispatch on `notification.type`); `jobGrouping.ts` groups a file's records
 * and this module derives the card: task chips, transcription sub-stages (from the backend's
 * `stage` key), sub-task durations, and a weighted overall percent whose LLM share depends on
 * whether an LLM is available. Design and the event inventory:
 * `docs/design/consolidated_processing_notification_plan.md`.
 */
import type { Notification } from '$stores/websocket';
import { formatCompactDuration } from '$lib/utils/formatting';
import { JOB_TASK_BY_TYPE, type JobItem, type MemberTask } from './jobGrouping';
import { taskStateFromStatus, type StepMark, type TaskState } from './runTiming';

export type { TaskState } from './runTiming';

export type JobTaskKey =
  | 'import'
  | 'transcription'
  | 'diarization'
  | 'redaction'
  | 'enrichment'
  | 'summary'
  | 'topics';

/**
 * Share of the overall bar. Diarization is reported inside the transcription percent and
 * enrichment has no reliable "all finished" signal, so neither carries weight of its own.
 */
export const TASK_WEIGHTS: Record<JobTaskKey, number> = {
  import: 10,
  transcription: 60,
  diarization: 0,
  redaction: 5,
  enrichment: 0,
  summary: 20,
  topics: 10,
};

export const TASK_ORDER: readonly JobTaskKey[] = [
  'import',
  'transcription',
  'diarization',
  'redaction',
  'enrichment',
  'summary',
  'topics',
];

export const LLM_TASKS: readonly JobTaskKey[] = ['summary', 'topics'];

/**
 * How long an expected-but-unannounced LLM task holds a finished transcription open. The
 * backend is silent when a user turned automatic topic extraction off, so without this the
 * card would wait forever.
 */
export const LLM_START_GRACE_MS = 3 * 60_000;

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
  state: TaskState;
  percent: number;
  weight: number;
  durationSeconds: number | null;
  message?: string;
  subtasks: JobSubtask[];
}

export type JobState = 'running' | 'done' | 'failed';

export interface ProcessingJobModel {
  key: string;
  fileId: string;
  filename: string;
  state: JobState;
  percent: number;
  tasks: JobTask[];
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

/**
 * Sub-stage steps from the stage the backend sent on each frame (recorded on the step log by
 * `runTiming`). The current stage is the latest one seen: a frame without a stage leaves it
 * where it was, and a record with none at all is at its start. Time is attributed to a stage from each mark
 * until the next mark of a different stage, or until the run finished, and summed, so a stage
 * the backend revisits is still timed correctly.
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
  return {
    key,
    state,
    percent,
    weight: TASK_WEIGHTS[key],
    durationSeconds: state === 'done' ? taskDuration(n) : null,
    message: state === 'running' || state === 'failed' ? messageOf(n) : undefined,
    subtasks: [],
  };
}

function pendingTask(key: JobTaskKey): JobTask {
  return {
    key,
    state: 'pending',
    percent: 0,
    weight: TASK_WEIGHTS[key],
    durationSeconds: null,
    subtasks: [],
  };
}

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
  const txStart = tx?.startedAt;
  // A downstream record older than the current transcription run is from a previous run.
  const current = (task: MemberTask) => {
    const rec = byTask.get(task);
    if (!rec || (txStart !== undefined && ms(rec.timestamp) < txStart)) return undefined;
    return rec;
  };

  const tasks: JobTask[] = [];
  let importState: TaskState | undefined;
  if (imp) {
    const t = recordTask('import', imp, percentOf(imp));
    importState = t.state;
    tasks.push(t);
  }

  let txState: TaskState | undefined;
  if (tx) {
    const t = recordTask('transcription', tx, percentOf(tx));
    txState = t.state;
    t.subtasks = transcriptionStages(tx, t.state);
    tasks.push(t);

    // The diarization chip is the diarize step, shown at task level.
    const diarize = t.subtasks[DIARIZE_INDEX];
    tasks.push({
      key: 'diarization',
      state: diarize.state,
      percent: diarize.state === 'done' ? 100 : 0,
      weight: TASK_WEIGHTS.diarization,
      durationSeconds: diarize.durationSeconds,
      subtasks: [],
    });

    const expected = tx.enrichmentTasks ?? [];
    if (expected.length > 0) {
      const done = tx.enrichmentCompletedAt ?? {};
      const subtasks: JobSubtask[] = expected.map((name) => ({
        key: name,
        state: done[name] !== undefined ? 'done' : 'running',
        durationSeconds: seconds(tx.enrichmentStartedAt, done[name]),
      }));
      const allDone = subtasks.every((s) => s.state === 'done');
      tasks.push({
        key: 'enrichment',
        state: allDone ? 'done' : 'running',
        percent: allDone ? 100 : 0,
        weight: TASK_WEIGHTS.enrichment,
        durationSeconds: null,
        subtasks,
      });
    }
  } else if (imp && importState !== 'failed') {
    txState = 'pending';
    tasks.push(pendingTask('transcription'));
  }

  const redaction = current('redaction');
  if (redaction) tasks.push(recordTask('redaction', redaction, null));

  const pipelineAlive = txState !== undefined && txState !== 'failed' && txState !== 'skipped';
  const graceOver =
    txState === 'done' &&
    tx?.finishedAt !== undefined &&
    opts.now - tx.finishedAt > LLM_START_GRACE_MS;
  for (const key of ['summary', 'topics'] as const) {
    const rec = current(key);
    if (rec) {
      tasks.push(recordTask(key, rec, key === 'summary' ? percentOf(rec) : null));
    } else if (opts.llmAvailable && pipelineAlive && !graceOver) {
      tasks.push(pendingTask(key));
    }
  }

  const visible = tasks
    .filter((t) => t.state !== 'skipped')
    .sort((a, b) => TASK_ORDER.indexOf(a.key) - TASK_ORDER.indexOf(b.key));

  const counted = visible.filter((t) => t.weight > 0);
  const failedTask = visible.find((t) => t.state === 'failed');
  const unfinished = counted.some((t) => t.state === 'pending' || t.state === 'running');
  const state: JobState = failedTask ? 'failed' : unfinished ? 'running' : 'done';

  const totalWeight = counted.reduce((sum, t) => sum + t.weight, 0);
  const progressOf = (t: JobTask) =>
    t.state === 'done' ? 100 : t.state === 'running' || t.state === 'failed' ? t.percent : 0;
  let percent =
    totalWeight > 0
      ? Math.round(counted.reduce((sum, t) => sum + t.weight * progressOf(t), 0) / totalWeight)
      : 0;
  if (state === 'done') percent = 100;
  else if (state === 'running') percent = Math.min(99, percent);

  const runningTask = visible.find((t) => t.state === 'running' && t.message);
  const newest = item.members[0];
  const filename =
    (item.members.map((m) => m.data?.filename).find((f) => typeof f === 'string' && f) as
      | string
      | undefined) ?? '';
  const eta =
    tx &&
    txState === 'running' &&
    typeof tx.progress?.etaSeconds === 'number' &&
    tx.progress.etaSeconds > 0
      ? formatCompactDuration(tx.progress.etaSeconds)
      : undefined;

  return {
    key: item.key,
    fileId: item.fileId,
    filename,
    state,
    percent,
    tasks: visible,
    memberIds: item.members.map((m) => m.id),
    read: item.members.every((m) => m.read),
    dismissible: item.members.every((m) => m.dismissible === true),
    timestamp: newest.timestamp,
    currentStep: state === 'running' ? runningTask?.message : undefined,
    etaDisplay: eta,
    failure: failedTask ? { task: failedTask.key, message: failedTask.message ?? '' } : undefined,
  };
}
