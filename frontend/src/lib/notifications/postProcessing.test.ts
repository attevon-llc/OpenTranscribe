/**
 * The post-processing reducer: one run per file, ordered by SERVER time.
 *
 * DEFECTS THESE CATCH: a frame that arrives late overwriting a newer state (a "started"
 * landing after "done" would re-open a finished task forever); a reprocess showing the
 * previous run's results as this run's; a task that starts before its plan lands being
 * lost when the plan arrives; a stray frame for a file with no card creating one; and a
 * finished run staying pinned (or a running one becoming clearable).
 */
import { describe, it, expect } from 'vitest';
import type { Notification } from '$stores/websocket';
import {
  mergePostProcessing,
  postProcessingOutstanding,
  upsertPostProcessingRecord,
  type PostProcessingRun,
} from './postProcessing';
import { isAutoClearable } from './retention';

const NOW = 1_800_000_000_000;

function plan(runAt: number, tasks: Record<string, string>) {
  return {
    file_id: 'f1',
    run_at: runAt,
    tasks: Object.fromEntries(Object.entries(tasks).map(([k, state]) => [k, { state, at: runAt }])),
  };
}

function frame(task: string, state: string, at: number, started_at?: number) {
  return { file_id: 'f1', tasks: { [task]: { state, at, started_at } } };
}

describe('mergePostProcessing', () => {
  it('last write by server time wins; a late, older frame is ignored', () => {
    let run = mergePostProcessing(undefined, plan(100, { analytics: 'queued' }), NOW);
    run = mergePostProcessing(run, frame('analytics', 'started', 101), NOW);
    run = mergePostProcessing(run, frame('analytics', 'done', 105), NOW);
    run = mergePostProcessing(run, frame('analytics', 'started', 102), NOW + 1);

    expect(run.tasks.analytics).toEqual({ state: 'done', at: 105, startedAt: 101 });
    expect(run.updatedAt).toBe(NOW + 1);
  });

  it('a new plan drops the previous run but keeps a task of the new run that started first', () => {
    let run = mergePostProcessing(undefined, plan(100, { summary: 'queued' }), NOW);
    run = mergePostProcessing(run, frame('summary', 'done', 120), NOW);
    run = mergePostProcessing(run, frame('file_facts', 'started', 201), NOW);
    run = mergePostProcessing(run, plan(200, { file_facts: 'queued', topics: 'skipped' }), NOW);

    expect(run.runAt).toBe(200);
    expect(run.tasks).toEqual({
      file_facts: { state: 'started', at: 201, startedAt: 201 },
      topics: { state: 'skipped', at: 200 },
    });
  });

  it('an older plan cannot reopen a newer run', () => {
    const newer = mergePostProcessing(undefined, plan(300, { topics: 'queued' }), NOW);
    const after = mergePostProcessing(newer, plan(250, { redaction: 'queued' }), NOW + 5);
    expect(after).toBe(newer);
  });

  it('ignores an unknown state or a frame with no time', () => {
    const run = mergePostProcessing(
      undefined,
      { file_id: 'f1', tasks: { a: { state: 'exploded', at: 1 }, b: { state: 'done' } } },
      NOW
    );
    expect(run.tasks).toEqual({});
  });

  it('keeps the server start time a terminal frame carries', () => {
    const run = mergePostProcessing(undefined, frame('redaction', 'failed', 50, 41), NOW);
    expect(run.tasks.redaction).toEqual({ state: 'failed', at: 50, startedAt: 41 });
  });

  it('a snapshot with no run marks the file unknown; one with a run does not', () => {
    expect(
      mergePostProcessing(undefined, { file_id: 'f1', run_at: null, tasks: {} }, NOW).unknown
    ).toBe(true);
    expect(mergePostProcessing(undefined, plan(1, {}), NOW).unknown).toBeUndefined();
  });
});

describe('postProcessingOutstanding', () => {
  const run = (over: Partial<PostProcessingRun>): PostProcessingRun => ({
    runAt: 1,
    tasks: {},
    updatedAt: NOW,
    ...over,
  });

  it('is outstanding while any task is queued or started', () => {
    expect(postProcessingOutstanding(run({ tasks: { a: { state: 'queued', at: 2 } } }))).toBe(true);
    expect(postProcessingOutstanding(run({ tasks: { a: { state: 'started', at: 2 } } }))).toBe(
      true
    );
    expect(
      postProcessingOutstanding(
        run({ tasks: { a: { state: 'done', at: 2 }, b: { state: 'skipped', at: 2 } } })
      )
    ).toBe(false);
  });

  it('is outstanding before any plan, but not once the server says it knows none', () => {
    expect(postProcessingOutstanding(run({ runAt: null }))).toBe(true);
    expect(postProcessingOutstanding(run({ runAt: null, unknown: true }))).toBe(false);
  });
});

describe('upsertPostProcessingRecord', () => {
  const tx: Notification = {
    id: 'tx',
    type: 'transcription_status',
    progressId: 'transcription_status_f1',
    title: '',
    message: '',
    timestamp: new Date(NOW - 1000),
    read: true,
    status: 'completed',
    dismissible: true,
    data: { file_id: 'f1', status: 'completed' },
  };
  const ids = () => 'pp1';

  it('creates the record beside an existing transcription record, newest first', () => {
    const next = upsertPostProcessingRecord([tx], plan(10, { analytics: 'queued' }), NOW, ids, 'T');
    expect(next?.map((n) => n.id)).toEqual(['pp1', 'tx']);
    expect(next?.[0]).toMatchObject({
      type: 'post_processing_status',
      progressId: 'post_processing_status_f1',
      status: 'processing',
      dismissible: true,
      read: false,
      data: { file_id: 'f1', status: 'processing' },
    });
  });

  it('drops a frame for a file with no card, unless the card asked (force)', () => {
    expect(upsertPostProcessingRecord([], plan(10, {}), NOW, ids, 'T')).toBeNull();
    expect(upsertPostProcessingRecord([], plan(10, {}), NOW, ids, 'T', true)).toHaveLength(1);
    expect(upsertPostProcessingRecord([tx], { tasks: {} }, NOW, ids, 'T')).toBeNull();
  });

  it('is pinned by retention while outstanding, clearable once finished, kept when failed', () => {
    const open = upsertPostProcessingRecord([tx], plan(10, { analytics: 'queued' }), NOW, ids, 'T');
    expect(open && isAutoClearable(open[0])).toBe(false);

    const done = upsertPostProcessingRecord(
      open ?? [],
      frame('analytics', 'done', 11),
      NOW,
      ids,
      'T'
    );
    expect(done?.[0].status).toBe('completed');
    expect(done && isAutoClearable(done[0])).toBe(true);

    const failed = upsertPostProcessingRecord(
      open ?? [],
      frame('analytics', 'failed', 12),
      NOW,
      ids,
      'T'
    );
    expect(failed?.[0].status).toBe('error');
    expect(failed && isAutoClearable(failed[0])).toBe(false);
  });

  it('keeps the read flag through intermediate frames and clears it when the run finishes', () => {
    const open = upsertPostProcessingRecord(
      [tx],
      plan(10, { analytics: 'queued', file_facts: 'queued' }),
      NOW,
      ids,
      'T'
    );
    const read = (open ?? []).map((n) => ({ ...n, read: true, readAt: new Date(NOW) }));
    const mid = upsertPostProcessingRecord(read, frame('analytics', 'done', 11), NOW, ids, 'T');
    expect(mid?.[0].read).toBe(true);
    const end = upsertPostProcessingRecord(
      mid ?? [],
      frame('file_facts', 'done', 12),
      NOW,
      ids,
      'T'
    );
    expect(end?.[0].read).toBe(false);
    expect(end?.map((n) => n.id)).toEqual(['pp1', 'tx']);
  });
});
