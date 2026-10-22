/**
 * Run timing reducer for progressive notification records.
 *
 * DEFECTS THESE CATCH: a retry or reprocess reusing the previous run's start time (every
 * stage duration of the new run would include the old one), a repeated completion frame
 * stretching the finish time, a first frame that is already terminal fabricating a "took 0s"
 * duration, and an unbounded step log growing localStorage without limit.
 */
import { describe, it, expect } from 'vitest';
import {
  STEP_LOG_LIMIT,
  advanceRunTiming,
  isTerminalState,
  taskStateFromStatus,
  type TaskState,
} from './runTiming';

const T0 = Date.UTC(2026, 9, 21, 12, 0, 0);

describe('taskStateFromStatus', () => {
  it.each<[string | undefined, TaskState]>([
    ['completed', 'done'],
    ['success', 'done'],
    ['done', 'done'],
    ['finished', 'done'],
    ['error', 'failed'],
    ['failed', 'failed'],
    ['disabled', 'skipped'],
    ['not_configured', 'skipped'],
    ['skipped', 'skipped'],
    ['pending', 'pending'],
    ['queued', 'pending'],
    ['processing', 'running'],
    ['generating', 'running'],
    [undefined, 'running'],
  ])('%s -> %s', (status, state) => {
    expect(taskStateFromStatus(status)).toBe(state);
  });

  it('treats done, failed and skipped as terminal and nothing else', () => {
    expect((['done', 'failed', 'skipped'] as TaskState[]).every(isTerminalState)).toBe(true);
    expect(isTerminalState('running')).toBe(false);
    expect(isTerminalState('pending')).toBe(false);
  });
});

describe('advanceRunTiming', () => {
  it('starts a run on the first frame', () => {
    const { timing, newRun } = advanceRunTiming(undefined, undefined, 'processing', 10, T0);
    expect(newRun).toBe(true);
    expect(timing).toEqual({ startedAt: T0, finishedAt: undefined, stepLog: [{ p: 10, at: T0 }] });
  });

  it('does not invent a start when the first frame seen is already the end', () => {
    const { timing } = advanceRunTiming(undefined, undefined, 'completed', 100, T0);
    expect(timing.startedAt).toBeUndefined();
    expect(timing.finishedAt).toBe(T0);
  });

  it('appends a mark only when the progress value changes', () => {
    let state = advanceRunTiming(undefined, undefined, 'processing', 10, T0).timing;
    state = advanceRunTiming(state, 'processing', 'processing', 10, T0 + 1000).timing;
    state = advanceRunTiming(state, 'processing', 'processing', 25, T0 + 2000).timing;
    expect(state.stepLog).toEqual([
      { p: 10, at: T0 },
      { p: 25, at: T0 + 2000 },
    ]);
    expect(state.startedAt).toBe(T0);
  });

  it('records the backend stage on the mark, and a stage change alone adds a mark', () => {
    let state = advanceRunTiming(undefined, undefined, 'processing', 52, T0, 'diarize').timing;
    state = advanceRunTiming(state, 'processing', 'processing', 52, T0 + 1000, 'diarize').timing;
    state = advanceRunTiming(state, 'processing', 'processing', 52, T0 + 2000, 'finalize').timing;
    expect(state.stepLog).toEqual([
      { p: 52, at: T0, stage: 'diarize' },
      { p: 52, at: T0 + 2000, stage: 'finalize' },
    ]);
  });

  it('a frame without a stage keeps the current stage', () => {
    let state = advanceRunTiming(undefined, undefined, 'processing', 40, T0, 'transcribe').timing;
    state = advanceRunTiming(state, 'processing', 'processing', 45, T0 + 1000).timing;
    state = advanceRunTiming(state, 'processing', 'completed', 100, T0 + 2000).timing;
    expect(state.stepLog?.map((m) => m.stage)).toEqual(['transcribe', 'transcribe', 'transcribe']);
  });

  it('a new run does not inherit the previous run stage', () => {
    let state = advanceRunTiming(undefined, undefined, 'processing', 95, T0, 'speakers').timing;
    state = advanceRunTiming(state, 'processing', 'completed', 100, T0 + 1000).timing;
    const next = advanceRunTiming(state, 'completed', 'processing', 0, T0 + 5000);
    expect(next.newRun).toBe(true);
    expect(next.timing.stepLog).toEqual([{ p: 0, at: T0 + 5000 }]);
  });

  it('records the FIRST terminal frame and keeps it on a repeated one', () => {
    let state = advanceRunTiming(undefined, undefined, 'processing', 50, T0).timing;
    state = advanceRunTiming(state, 'processing', 'completed', 100, T0 + 5000).timing;
    state = advanceRunTiming(state, 'completed', 'completed', 100, T0 + 9000).timing;
    expect(state.finishedAt).toBe(T0 + 5000);
    expect(state.startedAt).toBe(T0);
  });

  it('starts over when work resumes after an end (retry / reprocess)', () => {
    let state = advanceRunTiming(undefined, undefined, 'processing', 50, T0).timing;
    state = advanceRunTiming(state, 'processing', 'error', 50, T0 + 5000).timing;
    const next = advanceRunTiming(state, 'error', 'processing', 0, T0 + 60_000);
    expect(next.newRun).toBe(true);
    expect(next.timing).toEqual({
      startedAt: T0 + 60_000,
      finishedAt: undefined,
      stepLog: [{ p: 0, at: T0 + 60_000 }],
    });
  });

  it('bounds the step log and keeps the run start', () => {
    let state = advanceRunTiming(undefined, undefined, 'processing', 0, T0).timing;
    for (let p = 1; p <= STEP_LOG_LIMIT + 10; p += 1) {
      state = advanceRunTiming(state, 'processing', 'processing', p, T0 + p).timing;
    }
    expect(state.stepLog).toHaveLength(STEP_LOG_LIMIT);
    expect(state.stepLog?.[0]).toEqual({ p: 0, at: T0 });
    expect(state.stepLog?.[STEP_LOG_LIMIT - 1].p).toBe(STEP_LOG_LIMIT + 10);
  });
});
