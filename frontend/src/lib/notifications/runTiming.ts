/**
 * Run timing for progressive notification records (consolidated processing card).
 *
 * The backend sends the latest progress, message and (for transcription) sub-stage per frame,
 * so the per-stage and per-task durations the processing card shows have to be observed as
 * frames arrive. This
 * module is the pure reducer `stores/websocket.ts` calls on every progressive frame; it takes
 * an injected `now` so it is testable without fake timers.
 */

export type TaskState = 'pending' | 'running' | 'done' | 'failed' | 'skipped';

/**
 * One observed progress value and when it arrived (epoch ms). `stage` is the sub-stage the
 * backend filed the frame under; a frame without one keeps the previous mark's stage.
 */
export interface StepMark {
  p: number;
  at: number;
  stage?: string;
}

export interface RunTiming {
  /** First frame of the current run; absent when the first frame seen was already terminal. */
  startedAt?: number;
  /** First terminal frame of the current run. */
  finishedAt?: number;
  stepLog?: StepMark[];
}

/** Bounded so localStorage stays small; the first mark (the run start) is always kept. */
export const STEP_LOG_LIMIT = 40;

const DONE = new Set(['completed', 'success', 'complete', 'finished', 'done']);
const FAILED = new Set(['error', 'failed']);
const SKIPPED = new Set(['disabled', 'not_configured', 'skipped']);
const PENDING = new Set(['pending', 'queued']);

/** Maps the backend's wire status vocabulary onto the card's five task states. */
export function taskStateFromStatus(status: string | null | undefined): TaskState {
  const s = (status ?? '').toLowerCase();
  if (DONE.has(s)) return 'done';
  if (FAILED.has(s)) return 'failed';
  if (SKIPPED.has(s)) return 'skipped';
  if (PENDING.has(s)) return 'pending';
  return 'running';
}

export function isTerminalState(state: TaskState): boolean {
  return state === 'done' || state === 'failed' || state === 'skipped';
}

export interface AdvanceResult {
  timing: RunTiming;
  /** True when this frame starts a new run (first frame, or a retry/reprocess after an end). */
  newRun: boolean;
}

/**
 * Fold one frame into a record's timing. A non-terminal frame on a record whose previous
 * status was terminal is a new run (retry or reprocess), so its timing starts over. A mark is
 * added when the progress value or the stage changes.
 */
export function advanceRunTiming(
  prev: RunTiming | undefined,
  prevStatus: string | undefined,
  status: string,
  progress: number,
  now: number,
  stage?: string
): AdvanceResult {
  const terminal = isTerminalState(taskStateFromStatus(status));
  const prevTerminal = prevStatus !== undefined && isTerminalState(taskStateFromStatus(prevStatus));
  const newRun = prev === undefined || prevStatus === undefined || (prevTerminal && !terminal);

  if (newRun) {
    return {
      newRun,
      timing: {
        startedAt: terminal ? undefined : now,
        finishedAt: terminal ? now : undefined,
        stepLog: [stage ? { p: progress, at: now, stage } : { p: progress, at: now }],
      },
    };
  }

  let stepLog = prev.stepLog ? [...prev.stepLog] : [];
  const last = stepLog.length ? stepLog[stepLog.length - 1] : undefined;
  const markStage = stage || last?.stage;
  if (!last || last.p !== progress || markStage !== last.stage) {
    stepLog.push(markStage ? { p: progress, at: now, stage: markStage } : { p: progress, at: now });
    if (stepLog.length > STEP_LOG_LIMIT) {
      stepLog = [stepLog[0], ...stepLog.slice(stepLog.length - (STEP_LOG_LIMIT - 1))];
    }
  }
  return {
    newRun,
    timing: {
      startedAt: prev.startedAt,
      finishedAt: terminal ? prev.finishedAt ?? now : undefined,
      stepLog,
    },
  };
}
