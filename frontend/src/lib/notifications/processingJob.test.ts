/**
 * Consolidated processing card model.
 *
 * DEFECTS THESE CATCH: one file still producing several panel items; the card saying
 * "All tasks complete" while speaker attributes, search indexing or other post-processing
 * was still running (the reported bug: those tasks carried no weight and had no completion
 * signal); a card claiming completion when no plan of the final steps was ever seen; a
 * skipped task (feature off, no LLM) holding the card open forever; a failed post-processing
 * task reading as success; a reprocess showing the previous run's summary as done; and
 * durations taken from the recording length instead of the processing time.
 */
import { describe, it, expect } from 'vitest';
import type { Notification, NotificationType } from '$stores/websocket';
import {
  PLAN_GRACE_MS,
  STALE_AFTER_MS,
  buildJobModel,
  type JobTaskKey,
  type ProcessingJobModel,
} from './processingJob';
import type { PostProcessingRun } from './postProcessing';
import { countUnreadItems, groupPanelItems, jobKeyOf, type JobItem } from './jobGrouping';

const NOW = Date.UTC(2026, 9, 21, 12, 0, 0);
const S = 1000;
/** Server clock of the plan (epoch seconds); only ordering matters to the model. */
const RUN = NOW / 1000 - 50;

let seq = 0;
function rec(
  type: NotificationType,
  status: string,
  over: Partial<Notification> & { fileId?: string; data?: Notification['data'] } = {}
): Notification {
  const { fileId = 'f1', data, ...rest } = over;
  seq += 1;
  return {
    id: `n${seq}`,
    type,
    title: type,
    message: '',
    timestamp: new Date(NOW - 10 * S),
    read: false,
    status: status as Notification['status'],
    dismissible: ['completed', 'error', 'failed', 'done', 'disabled', 'not_configured'].includes(
      status
    ),
    data: { file_id: fileId, filename: 'meeting.mp3', status, ...data },
    ...rest,
  };
}

type EntryState = 'queued' | 'started' | 'done' | 'failed' | 'skipped';

/** A post-processing record: a plan (runAt) plus task states at server times after it. */
function pp(
  tasks: Partial<Record<string, EntryState>>,
  over: Partial<PostProcessingRun> = {},
  fileId = 'f1'
): Notification {
  const run: PostProcessingRun = {
    runAt: RUN,
    tasks: Object.fromEntries(
      Object.entries(tasks).map(([k, state]) => [
        k,
        state === 'done' || state === 'failed'
          ? { state, at: RUN + 10, startedAt: RUN + 2 }
          : { state: state as EntryState, at: RUN + 1 },
      ])
    ),
    updatedAt: NOW - 5 * S,
    ...over,
  };
  return {
    ...rec('post_processing_status', 'processing', { fileId, timestamp: new Date(NOW - 5 * S) }),
    dismissible: true,
    postProcessing: run,
  };
}

function jobOf(list: Notification[]): JobItem {
  const item = groupPanelItems(list).find((i) => i.kind === 'job');
  if (!item || item.kind !== 'job') throw new Error('no job item');
  return item;
}

function model(list: Notification[], llmAvailable: boolean, now = NOW): ProcessingJobModel {
  return buildJobModel(jobOf(list), { llmAvailable, now });
}

function chips(m: ProcessingJobModel): Array<[JobTaskKey, string]> {
  return m.tasks.map((t) => [t.key, t.state]);
}

function tiers(m: ProcessingJobModel): Array<[string, string]> {
  return m.tiers.map((t) => [t.key, t.state]);
}

const txDone = (over: Partial<Notification> = {}) =>
  rec('transcription_status', 'completed', {
    startedAt: NOW - 300 * S,
    finishedAt: NOW - 60 * S,
    progress: { current: 100, total: 100, percentage: 100 },
    ...over,
  });

/** Every finishing task the default pipeline runs, all done. */
const FINISHING_DONE: Partial<Record<string, EntryState>> = {
  search_indexing: 'done',
  file_facts: 'done',
  analytics: 'done',
  redaction: 'skipped',
  speaker_attributes: 'done',
  speaker_identification: 'done',
  speaker_clustering: 'done',
};

describe('grouping', () => {
  it('folds every processing event of one file into ONE item, newest first', () => {
    const list = [
      rec('topic_extraction_status', 'processing'),
      rec('summarization_status', 'processing'),
      rec('collection_shared', 'completed', { fileId: undefined, data: {} }),
      rec('redaction_status', 'done'),
      rec('transcription_status', 'completed'),
      rec('transcription_status', 'processing', { fileId: 'f2' }),
    ];

    const items = groupPanelItems(list);

    expect(items.map((i) => i.key)).toEqual(['job:f1', list[2].id, 'job:f2']);
    const first = items[0];
    expect(first.kind === 'job' && first.members.map((m) => m.type)).toEqual([
      'topic_extraction_status',
      'summarization_status',
      'redaction_status',
      'transcription_status',
    ]);
  });

  it('keeps silent records out, and batch-scoped auto-label as a row of its own', () => {
    const silent = rec('transcription_status', 'processing', { silent: true });
    const autoLabel = rec('auto_label_status', 'processing', { fileId: 'auto_label_batch' });

    expect(jobKeyOf(silent)).toBeNull();
    expect(jobKeyOf(autoLabel)).toBeNull();
    expect(groupPanelItems([silent, autoLabel])).toEqual([
      { kind: 'single', key: autoLabel.id, notification: autoLabel },
    ]);
  });

  it('counts a card once in the unread badge however many records are unread', () => {
    const list = [
      rec('transcription_status', 'completed'),
      rec('summarization_status', 'completed'),
      rec('topic_extraction_status', 'completed'),
      rec('reindex_complete', 'completed', { fileId: undefined, data: {} }),
    ];
    expect(countUnreadItems(list)).toBe(2);
    expect(countUnreadItems(list.map((n) => ({ ...n, read: true })))).toBe(0);
    // One unread record is enough to make the card unread.
    expect(countUnreadItems([{ ...list[0], read: true }, list[1]])).toBe(1);
  });
});

describe('THE BUG: complete means every known task is finished', () => {
  it('AI done but speaker attributes still running reads "Finishing up", never complete', () => {
    const m = model(
      [
        pp({
          ...FINISHING_DONE,
          speaker_attributes: 'started',
          speaker_identification: undefined,
          summary: 'done',
          topics: 'done',
        }),
        txDone(),
      ],
      true
    );

    expect(m.state).toBe('finishing');
    expect(m.outstanding).toEqual(['speaker_attributes']);
    expect(m.percent).toBeLessThan(100);
    expect(tiers(m)).toEqual([
      ['main', 'done'],
      ['finishing', 'running'],
      ['ai', 'done'],
    ]);
  });

  it('a queued task holds the card exactly like a running one', () => {
    const m = model([pp({ ...FINISHING_DONE, search_indexing: 'queued' }), txDone()], false);
    expect(m.state).toBe('finishing');
    expect(m.outstanding).toEqual(['search_indexing']);
    expect(m.percent).toBeLessThanOrEqual(99);
  });

  it('completes only when every planned task is done or skipped', () => {
    const m = model([pp({ ...FINISHING_DONE, summary: 'done', topics: 'done' }), txDone()], true);
    expect(m.state).toBe('complete');
    expect(m.percent).toBe(100);
    expect(m.outstanding).toEqual([]);
  });

  it('a task declared after the plan (a hand-off) re-opens the card until it finishes', () => {
    const declared = pp({ ...FINISHING_DONE, summary: 'done', summary_indexing: 'queued' });
    expect(model([declared, txDone()], true).state).toBe('finishing');
    const finished = pp({ ...FINISHING_DONE, summary: 'done', summary_indexing: 'done' });
    expect(model([finished, txDone()], true).state).toBe('complete');
  });

  it('never claims completion before the plan arrives', () => {
    const m = model([txDone({ finishedAt: NOW - 10 * S })], false);
    expect(m.state).toBe('finishing');
    expect(m.awaitingPlan).toBe(true);
    expect(tiers(m)).toEqual([
      ['main', 'done'],
      ['finishing', 'running'],
    ]);
    // 60 * 100 / (60 + the 15 the unknown finishing work holds back)
    expect(m.percent).toBe(80);
  });

  it('without a plan after the grace period says it cannot confirm, not "complete"', () => {
    const m = model([txDone({ finishedAt: NOW - PLAN_GRACE_MS - S })], false);
    expect(m.state).toBe('ready');
    expect(m.awaitingPlan).toBe(false);
    expect(m.reconcile).toBe(true);
    expect(tiers(m)).toEqual([['main', 'done']]);
    // ...and not one second before the grace ends.
    expect(model([txDone({ finishedAt: NOW - PLAN_GRACE_MS + S })], false).state).toBe('finishing');
  });

  it('a server that knows no run for the file reads "ready" and stops asking', () => {
    const m = model(
      [pp({}, { runAt: null, unknown: true }), txDone({ finishedAt: NOW - 10 * S })],
      true
    );
    expect(m.state).toBe('ready');
    expect(m.reconcile).toBe(false);
  });
});

describe('skipped and failed tasks', () => {
  it('skipped tasks do not block and do not render', () => {
    const m = model(
      [pp({ ...FINISHING_DONE, summary: 'skipped', topics: 'skipped' }), txDone()],
      true
    );
    expect(m.state).toBe('complete');
    expect(chips(m).map(([k]) => k)).not.toContain('redaction');
    expect(chips(m).map(([k]) => k)).not.toContain('summary');
    // No AI tier when every AI task was skipped (no LLM, or switched off).
    expect(tiers(m).map(([k]) => k)).toEqual(['main', 'finishing']);
  });

  it('a failed task surfaces "needs attention" once nothing else is running', () => {
    const m = model([pp({ ...FINISHING_DONE, search_indexing: 'failed' }), txDone()], false);
    expect(m.state).toBe('attention');
    expect(m.failure).toEqual({ task: 'search_indexing', message: '' });
    expect(tiers(m)).toContainEqual(['finishing', 'failed']);
    expect(m.percent).toBeLessThan(100);
  });

  it('a failure while other tasks still run keeps "Finishing up" with the failed chip', () => {
    const m = model(
      [
        pp({ ...FINISHING_DONE, search_indexing: 'failed', speaker_clustering: 'started' }),
        txDone(),
      ],
      false
    );
    expect(m.state).toBe('finishing');
    expect(chips(m)).toContainEqual(['search_indexing', 'failed']);
    expect(m.failure).toBeUndefined();
  });

  it('carries the failed summary message from its own record', () => {
    const m = model(
      [
        pp({ ...FINISHING_DONE, summary: 'failed' }),
        rec('summarization_status', 'failed', {
          timestamp: new Date(NOW),
          message: 'Provider returned 500',
        }),
        txDone(),
      ],
      true
    );
    expect(m.state).toBe('attention');
    expect(m.failure).toEqual({ task: 'summary', message: 'Provider returned 500' });
  });
});

describe('missed frames', () => {
  it('outstanding work with no frame for a long time is flagged stale, not completed', () => {
    const stale = pp(
      { ...FINISHING_DONE, speaker_clustering: 'started' },
      { updatedAt: NOW - STALE_AFTER_MS - S }
    );
    const m = model([stale, txDone()], false);
    expect(m.state).toBe('finishing');
    expect(m.stale).toBe(true);
    expect(m.reconcile).toBe(true);
  });

  it('asks the server when the plan is late', () => {
    expect(model([txDone({ finishedAt: NOW - 45 * S })], false).reconcile).toBe(true);
    expect(model([txDone({ finishedAt: NOW - 5 * S })], false).reconcile).toBe(false);
  });

  it('a record saved before run timing existed waits from its timestamp, not forever', () => {
    const legacy = rec('transcription_status', 'completed', {
      timestamp: new Date(NOW - PLAN_GRACE_MS - S),
    });
    expect(model([legacy], false).state).toBe('ready');
  });
});

describe('the AI tier follows LLM availability and the plan', () => {
  it('no LLM configured: no AI tier at all', () => {
    const m = model([txDone({ finishedAt: NOW - 5 * S })], false);
    expect(tiers(m).map(([k]) => k)).not.toContain('ai');
  });

  it('LLM configured, before the plan: the AI tier waits', () => {
    const m = model([txDone({ finishedAt: NOW - 5 * S })], true);
    expect(chips(m)).toContainEqual(['summary', 'pending']);
    expect(chips(m)).toContainEqual(['topics', 'pending']);
    expect(tiers(m)).toContainEqual(['ai', 'pending']);
  });

  it('the plan decides: a skipped summary takes the AI tier away', () => {
    const m = model(
      [pp({ ...FINISHING_DONE, summary: 'skipped', topics: 'skipped' }), txDone()],
      true
    );
    expect(tiers(m).map(([k]) => k)).not.toContain('ai');
  });

  it('weights a running summary by its own progress', () => {
    const m = model(
      [
        pp({ ...FINISHING_DONE, summary: 'started', topics: 'done' }),
        rec('summarization_status', 'processing', {
          data: { progress: 50 },
          timestamp: new Date(NOW),
        }),
        txDone(),
      ],
      true
    );
    const summary = m.tasks.find((t) => t.key === 'summary');
    expect(summary?.state).toBe('running');
    expect(summary?.percent).toBe(50);
    expect(m.state).toBe('finishing');
  });

  it('a summary regenerated on an old file is a card with just the summary', () => {
    const m = model([rec('summarization_status', 'processing', { data: { progress: 30 } })], true);
    expect(chips(m)).toEqual([['summary', 'running']]);
    expect(m.percent).toBe(30);
    expect(m.state).toBe('processing');
  });
});

describe('main processing', () => {
  it('never shows 100% while transcription is still running', () => {
    const m = model(
      [
        rec('transcription_status', 'processing', {
          progress: { current: 100, total: 100, percentage: 100 },
        }),
      ],
      false
    );
    expect(m.state).toBe('processing');
    // Transcription at 100% is not the whole job: the finishing tier still holds its share.
    expect(m.percent).toBe(80);
  });
});

describe('transcription steps and durations', () => {
  const t0 = NOW - 200 * S;
  const marks = [
    { p: 5, at: t0, stage: 'prepare' },
    { p: 25, at: t0 + 10 * S, stage: 'transcribe' },
    { p: 43, at: t0 + 20 * S, stage: 'transcribe' },
    { p: 52, at: t0 + 80 * S, stage: 'diarize' },
    { p: 65, at: t0 + 110 * S, stage: 'finalize' },
    { p: 80, at: t0 + 120 * S, stage: 'speakers' },
  ];

  it('derives each step duration from when the next step began', () => {
    const tx = rec('transcription_status', 'completed', {
      startedAt: t0,
      finishedAt: t0 + 150 * S,
      stepLog: [...marks, { p: 100, at: t0 + 150 * S, stage: 'speakers' }],
      data: { duration_seconds: 140 },
    });
    const m = model([tx], false);
    const transcription = m.tasks.find((t) => t.key === 'transcription');

    // The server-measured total wins over the client-observed 150 s.
    expect(transcription?.durationSeconds).toBe(140);
    expect(transcription?.subtasks.map((s) => [s.key, s.state, s.durationSeconds])).toEqual([
      ['prepare', 'done', 10],
      ['transcribe', 'done', 70],
      ['diarize', 'done', 30],
      ['finalize', 'done', 10],
      ['speakers', 'done', 30],
    ]);
    expect(m.tasks.find((t) => t.key === 'diarization')?.durationSeconds).toBe(30);
  });

  it('marks the live step running, earlier ones done, later ones waiting', () => {
    const tx = rec('transcription_status', 'processing', {
      startedAt: t0,
      stepLog: marks.slice(0, 4),
      progress: { current: 55, total: 100, percentage: 55, etaSeconds: 90 },
      currentStep: 'Analyzing speaker patterns',
    });
    const m = model([tx], false);
    const transcription = m.tasks.find((t) => t.key === 'transcription');

    expect(transcription?.subtasks.map((s) => s.state)).toEqual([
      'done',
      'done',
      'running',
      'pending',
      'pending',
    ]);
    expect(chips(m)).toContainEqual(['diarization', 'running']);
    // 60 * 55 / (60 + 15 held for the finishing tier)
    expect(m.percent).toBe(44);
    expect(m.currentStep).toBe('Analyzing speaker patterns');
    expect(m.etaDisplay).toBe('1m 30s');
  });

  it('a failure inside diarization fails that step and skips the rest, with the message', () => {
    const tx = rec('transcription_status', 'error', {
      startedAt: t0,
      finishedAt: t0 + 90 * S,
      stepLog: marks.slice(0, 4),
      progress: { current: 58, total: 100, percentage: 58 },
      message: 'Diarization ran out of memory',
    });
    const m = model([tx], true);

    expect(m.state).toBe('attention');
    expect(m.failure).toEqual({ task: 'transcription', message: 'Diarization ran out of memory' });
    // Nothing after a failed transcription will run, so no later tier waits.
    expect(tiers(m)).toEqual([['main', 'failed']]);
    expect(chips(m)).toEqual([
      ['transcription', 'failed'],
      ['diarization', 'failed'],
    ]);
    expect(m.tasks[0].subtasks.map((s) => s.state)).toEqual([
      'done',
      'done',
      'failed',
      'skipped',
      'skipped',
    ]);
  });

  // Seen in the panel: a transcription that failed at 0% drew a full bar labelled "100%"
  // under a red "A task failed" card, because a failed task counted as finished work.
  it('a failed job keeps the progress it reached, never a full 100% bar', () => {
    const atZero = rec('transcription_status', 'error', {
      progress: { current: 0, total: 100, percentage: 0 },
      message: 'Audio stream could not be decoded',
    });
    expect(model([atZero], false).percent).toBe(0);

    const midway = rec('transcription_status', 'error', {
      progress: { current: 58, total: 100, percentage: 58 },
      message: 'Diarization ran out of memory',
    });
    const m = model([midway], false);
    expect(m.state).toBe('attention');
    expect(m.percent).toBeGreaterThan(0);
    expect(m.percent).toBeLessThan(100);
  });

  it('takes the step from the backend stage, never from the percent', () => {
    // A cloud provider reports its own 0-100% inside transcription: 90% is still transcribing.
    const tx = rec('transcription_status', 'processing', {
      startedAt: t0,
      stepLog: [
        { p: 5, at: t0, stage: 'prepare' },
        { p: 90, at: t0 + 30 * S, stage: 'transcribe' },
      ],
      progress: { current: 90, total: 100, percentage: 90 },
    });
    const m = model([tx], false);

    expect(m.tasks[0].subtasks.map((s) => [s.key, s.state])).toEqual([
      ['prepare', 'done'],
      ['transcribe', 'running'],
      ['diarize', 'pending'],
      ['finalize', 'pending'],
      ['speakers', 'pending'],
    ]);
    expect(chips(m)).toContainEqual(['diarization', 'pending']);
  });

  it('sums the time of a stage the backend returns to', () => {
    const tx = rec('transcription_status', 'completed', {
      startedAt: t0,
      finishedAt: t0 + 100 * S,
      stepLog: [
        { p: 10, at: t0, stage: 'prepare' },
        { p: 40, at: t0 + 10 * S, stage: 'transcribe' },
        { p: 52, at: t0 + 30 * S, stage: 'diarize' },
        { p: 60, at: t0 + 35 * S, stage: 'transcribe' },
        { p: 65, at: t0 + 45 * S, stage: 'finalize' },
        { p: 95, at: t0 + 60 * S, stage: 'speakers' },
        { p: 100, at: t0 + 100 * S, stage: 'speakers' },
      ],
    });
    const durations = model([tx], false).tasks[0].subtasks.map((s) => s.durationSeconds);
    expect(durations).toEqual([10, 30, 5, 15, 40]);
  });

  it('a record without any stage sits at the first step, and the percent does not move it', () => {
    const tx = rec('transcription_status', 'processing', {
      startedAt: t0,
      stepLog: [
        { p: 10, at: t0 },
        { p: 70, at: t0 + 30 * S },
      ],
      progress: { current: 70, total: 100, percentage: 70 },
    });
    expect(model([tx], false).tasks[0].subtasks.map((s) => s.state)).toEqual([
      'running',
      'pending',
      'pending',
      'pending',
      'pending',
    ]);
  });

  it('THE TRAP: uses duration_seconds, never data.duration (the recording length)', () => {
    const m = model(
      [
        rec('summarization_status', 'completed', {
          timestamp: new Date(NOW),
          data: { duration_seconds: 45, duration: 5400 },
        }),
        txDone(),
      ],
      false
    );
    expect(m.tasks.find((t) => t.key === 'summary')?.durationSeconds).toBe(45);
  });

  it('falls back to the observed run time when the backend sends no duration', () => {
    const m = model(
      [
        rec('redaction_status', 'done', {
          timestamp: new Date(NOW),
          startedAt: NOW - 12 * S,
          finishedAt: NOW,
        }),
        txDone(),
      ],
      false
    );
    expect(m.tasks.find((t) => t.key === 'redaction')?.durationSeconds).toBe(12);
  });

  it('reports no duration rather than a fabricated one when nothing was observed', () => {
    const m = model([rec('transcription_status', 'completed')], false);
    expect(m.tasks[0].durationSeconds).toBeNull();
  });
});

describe('runs and pipelines', () => {
  it('treats a summary older than the current transcription run as a previous run', () => {
    const m = model(
      [
        rec('transcription_status', 'processing', {
          startedAt: NOW - 5 * S,
          timestamp: new Date(NOW),
          progress: { current: 30, total: 100, percentage: 30 },
        }),
        rec('summarization_status', 'completed', { timestamp: new Date(NOW - 3600 * S) }),
      ],
      true
    );
    expect(chips(m)).toContainEqual(['summary', 'pending']);
  });

  it('a URL import shows Import running and Transcription waiting', () => {
    const m = model(
      [
        rec('youtube_processing_status', 'processing', {
          progress: { current: 40, total: 100, percentage: 40 },
        }),
      ],
      false
    );
    expect(chips(m)).toEqual([
      ['import', 'running'],
      ['transcription', 'pending'],
    ]);
    // (10*40 + 60*0) / (70 + 15 held for the finishing tier)
    expect(m.percent).toBe(5);
  });
});

describe('card actions and identity', () => {
  it('is dismissible only when every record is, read only when every record is', () => {
    const done = [
      rec('summarization_status', 'completed'),
      rec('transcription_status', 'completed'),
    ];
    expect(model(done, false).dismissible).toBe(true);
    expect(model([rec('summarization_status', 'processing'), done[1]], false).dismissible).toBe(
      false
    );
    expect(model(done, false).read).toBe(false);
    expect(
      model(
        done.map((n) => ({ ...n, read: true })),
        false
      ).read
    ).toBe(true);
    expect(model(done, false).memberIds).toEqual(done.map((n) => n.id));
    expect(model(done, false).filename).toBe('meeting.mp3');
  });

  it('does not mutate the records', () => {
    const list = [txDone(), rec('summarization_status', 'processing')];
    const before = JSON.stringify(list);
    const m = model(list, true);
    expect(JSON.stringify(list)).toBe(before);
    expect(m.memberIds).toEqual(list.map((n) => n.id));
  });
});
