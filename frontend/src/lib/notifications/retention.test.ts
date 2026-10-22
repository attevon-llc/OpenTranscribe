/**
 * Retention policy for the notification inbox (issue #1217).
 *
 * DEFECTS THESE CATCH: an error, an in-flight item, an actionable notice, or an
 * unread item being auto-removed (silent loss of something the user must act on),
 * a new notification type defaulting to "clearable", and the cap evicting the
 * wrong rows. `pruneNotifications` takes an injected `now`, so no fake timers.
 */
import { describe, it, expect } from 'vitest';
import {
  MAX_NOTIFICATIONS,
  READ_RETENTION_MS,
  RETENTION_BY_TYPE,
  RETENTION_SETTINGS,
  UNREAD_RETENTION_MS,
  isAutoClearable,
  pruneNotifications,
  type PruneOptions,
  type RetentionSetting,
} from './retention';
import { NOTIFICATION_RETENTION_KEYS } from '$lib/i18n/keyMaps';
import { NOTIFICATION_TYPES, type Notification, type NotificationType } from '$stores/websocket';

const NOW = Date.UTC(2026, 9, 11, 12, 0, 0);
const HOUR = 3_600_000;
const DAY = 24 * HOUR;

let seq = 0;
function make(over: Partial<Notification> & { ageMs?: number; readAgoMs?: number }): Notification {
  const { ageMs = 0, readAgoMs, ...rest } = over;
  seq += 1;
  const n: Notification = {
    id: `n${seq}`,
    type: 'transcript_ready',
    title: 't',
    message: 'm',
    timestamp: new Date(NOW - ageMs),
    read: false,
    ...rest,
  };
  if (readAgoMs !== undefined) {
    n.read = true;
    n.readAt = new Date(NOW - readAgoMs);
  }
  return n;
}

function prune(list: Notification[], over: Partial<PruneOptions> = {}) {
  return pruneNotifications(list, {
    now: NOW,
    retention: 'read24h',
    panelOpen: false,
    ...over,
  });
}

function ids(list: Notification[]) {
  return list.map((n) => n.id);
}

describe('setting labels', () => {
  it('has a label key for every selectable retention setting', () => {
    expect(Object.keys(NOTIFICATION_RETENTION_KEYS).sort()).toEqual([...RETENTION_SETTINGS].sort());
  });
});

describe('type classification', () => {
  it('classifies every NotificationType explicitly (no silent default to clearable)', () => {
    const classified = Object.keys(RETENTION_BY_TYPE).sort();
    expect(classified).toEqual([...NOTIFICATION_TYPES].sort());
    for (const type of NOTIFICATION_TYPES) {
      expect(['informational', 'persistent']).toContain(RETENTION_BY_TYPE[type]);
    }
  });

  it('treats an unknown wire type as never auto-clearable', () => {
    const n = make({ type: 'brand_new_type' as NotificationType, readAgoMs: 100 * DAY });
    expect(isAutoClearable(n)).toBe(false);
    expect(prune([n]).kept).toEqual([n]);
  });

  it('keeps the actionable and system notices persistent', () => {
    const mustPersist: NotificationType[] = [
      'collection_shared',
      'collection_share_revoked',
      'collection_share_updated',
      'group_member_added',
      'group_member_removed',
      'file_takedown',
      'file_takedown_released',
      'reindex_complete',
      'reindex_progress',
      'migration_complete',
      'migration_finalized',
      'download_progress',
    ];
    for (const type of mustPersist) expect(RETENTION_BY_TYPE[type]).toBe('persistent');
  });
});

describe('what is never auto-removed', () => {
  const ancient = 400 * DAY;

  it('keeps errors however old and however long read', () => {
    const list = [
      make({ type: 'transcription_status', status: 'error', ageMs: ancient, readAgoMs: ancient }),
      make({
        type: 'summarization_status',
        status: 'completed',
        data: { status: 'failed' },
        ageMs: ancient,
        readAgoMs: ancient,
      }),
      make({
        type: 'topic_extraction_status',
        data: { status: 'not_configured' },
        ageMs: ancient,
        readAgoMs: ancient,
      }),
    ];
    expect(prune(list).kept).toEqual(list);
  });

  it('keeps in-flight items, read or not', () => {
    const list = [
      make({ type: 'transcription_status', status: 'processing', ageMs: ancient }),
      make({
        type: 'summarization_status',
        status: 'processing',
        ageMs: ancient,
        readAgoMs: ancient,
      }),
      make({ type: 'youtube_processing_status', data: { status: 'pending' }, ageMs: ancient }),
    ];
    expect(prune(list).kept).toEqual(list);
  });

  it('keeps actionable/system types even when read long ago', () => {
    const list = (
      [
        'collection_shared',
        'file_takedown',
        'reindex_complete',
        'migration_finalized',
      ] as NotificationType[]
    ).map((type) => make({ type, ageMs: ancient, readAgoMs: ancient }));
    expect(prune(list).kept).toEqual(list);
  });

  it('keeps unread successes younger than the unread limit', () => {
    const list = [
      make({ ageMs: UNREAD_RETENTION_MS - HOUR }),
      make({ type: 'transcription_status', status: 'completed', ageMs: 13 * DAY }),
    ];
    expect(prune(list).kept).toEqual(list);
  });

  it('keeps read successes younger than the read window, and read items with no readAt', () => {
    const young = make({ readAgoMs: READ_RETENTION_MS.read24h - HOUR });
    const unknownAge = make({ read: true, ageMs: ancient });
    expect(prune([young, unknownAge]).kept).toEqual([young, unknownAge]);
  });
});

describe('what is removed', () => {
  it('removes read successes 24 h after they were read (age from readAt, not creation)', () => {
    const gone = make({ ageMs: 2 * HOUR, readAgoMs: 24 * HOUR });
    const stays = make({ ageMs: 90 * DAY, readAgoMs: 23 * HOUR });
    const out = prune([stays, gone]);
    expect(ids(out.kept)).toEqual([stays.id]);
    expect(out.removedVisible).toBe(1);
  });

  it('removes a completed progress item once read for 24 h', () => {
    const done = make({
      type: 'transcription_status',
      status: 'completed',
      progressId: 'p1',
      readAgoMs: 25 * HOUR,
    });
    expect(prune([done]).kept).toEqual([]);
  });

  it('removes unread successes after 14 days only', () => {
    const old = make({ ageMs: UNREAD_RETENTION_MS });
    const fresh = make({ ageMs: UNREAD_RETENTION_MS - 1 });
    expect(ids(prune([fresh, old]).kept)).toEqual([fresh.id]);
  });

  it('does not count silent rows in removedVisible', () => {
    const silent = make({ silent: true, ageMs: UNREAD_RETENTION_MS });
    const out = prune([silent]);
    expect(out.kept).toEqual([]);
    expect(out.removedVisible).toBe(0);
  });

  it('does not mutate its input', () => {
    const list = [make({ readAgoMs: 30 * DAY }), make({})];
    const copy = [...list];
    prune(list);
    expect(list).toEqual(copy);
  });
});

describe('the setting', () => {
  const sevenAndAHalfDays = 7 * DAY + 12 * HOUR;

  it('read7d keeps a 3-day-read item that read24h removes', () => {
    const n = make({ readAgoMs: 3 * DAY });
    expect(prune([n], { retention: 'read24h' }).kept).toEqual([]);
    expect(prune([n], { retention: 'read7d' }).kept).toEqual([n]);
    expect(prune([make({ readAgoMs: sevenAndAHalfDays })], { retention: 'read7d' }).kept).toEqual(
      []
    );
  });

  it("'never' removes nothing by age, read or unread", () => {
    const list = [
      make({ readAgoMs: 900 * DAY }),
      make({ ageMs: 900 * DAY }),
      make({ ageMs: 900 * DAY, silent: true }),
    ];
    expect(prune(list, { retention: 'never' as RetentionSetting }).kept).toEqual(list);
  });

  it("'never' still enforces the hard cap on clearable rows (it bounds storage)", () => {
    const list = Array.from({ length: MAX_NOTIFICATIONS + 5 }, () => make({ readAgoMs: DAY }));
    expect(prune(list, { retention: 'never' }).kept).toHaveLength(MAX_NOTIFICATIONS);
  });
});

describe('panel-open deferral', () => {
  it('removes nothing while the panel is open, then prunes on the next call', () => {
    const list = [make({ readAgoMs: 30 * DAY }), make({ ageMs: 30 * DAY })];
    const open = prune(list, { panelOpen: true });
    expect(open.kept).toBe(list);
    expect(open.removedVisible).toBe(0);
    expect(prune(list, { panelOpen: false }).kept).toEqual([]);
  });

  it('defers the cap too', () => {
    const list = Array.from({ length: MAX_NOTIFICATIONS + 3 }, () => make({ readAgoMs: HOUR }));
    expect(prune(list, { panelOpen: true }).kept).toHaveLength(MAX_NOTIFICATIONS + 3);
  });
});

describe('hard cap', () => {
  // The list is newest-first, as the store keeps it.
  it('drops the oldest read informational first, then the oldest unread informational', () => {
    const unreadOld = make({ ageMs: 5 * DAY });
    const unreadNew = make({ ageMs: HOUR });
    const readOld = make({ ageMs: 9 * DAY, readAgoMs: HOUR });
    const readNew = make({ ageMs: 2 * DAY, readAgoMs: HOUR });
    const filler = Array.from({ length: MAX_NOTIFICATIONS - 4 }, (_, i) =>
      make({ type: 'file_takedown', ageMs: i * 1000 })
    );
    // 100 rows + 1 extra read row -> exactly one eviction, and it is the oldest read.
    const extra = make({ ageMs: 3 * DAY, readAgoMs: HOUR });
    const base = [unreadNew, readNew, extra, unreadOld, readOld, ...filler];
    expect(base).toHaveLength(MAX_NOTIFICATIONS + 1);
    const out = prune(base).kept;
    expect(ids(out)).not.toContain(readOld.id);
    expect(out).toHaveLength(MAX_NOTIFICATIONS);
    expect(ids(out)).toContain(unreadOld.id);

    // Two evictions: the two oldest READ rows go before any unread row.
    const out2 = prune([make({ ageMs: DAY, readAgoMs: HOUR }), ...base]).kept;
    expect(ids(out2)).not.toContain(readOld.id);
    expect(ids(out2)).not.toContain(extra.id);
    expect(ids(out2)).toContain(unreadOld.id);
    expect(ids(out2)).toContain(unreadNew.id);
  });

  it('falls through to the oldest unread informational when no read ones remain', () => {
    const oldest = make({ ageMs: 10 * DAY });
    const rest = Array.from({ length: MAX_NOTIFICATIONS }, (_, i) =>
      make({ ageMs: (i + 1) * HOUR })
    );
    const out = prune([...rest, oldest]).kept;
    expect(out).toHaveLength(MAX_NOTIFICATIONS);
    expect(ids(out)).not.toContain(oldest.id);
  });

  it('evicts invisible silent rows before any visible one', () => {
    const silent = make({ silent: true, ageMs: HOUR });
    const visibleOld = make({ ageMs: 9 * DAY, readAgoMs: HOUR });
    const rest = Array.from({ length: MAX_NOTIFICATIONS - 1 }, () =>
      make({ type: 'file_takedown' })
    );
    const out = prune([silent, visibleOld, ...rest]).kept;
    expect(ids(out)).not.toContain(silent.id);
    expect(ids(out)).toContain(visibleOld.id);
  });

  it('never drops errors, in-flight or actionable rows via the cap, even past 100', () => {
    const protectedRows = [
      ...Array.from({ length: 60 }, () => make({ type: 'file_takedown', readAgoMs: 99 * DAY })),
      ...Array.from({ length: 30 }, () =>
        make({ type: 'transcription_status', status: 'error', readAgoMs: 99 * DAY })
      ),
      ...Array.from({ length: 30 }, () =>
        make({ type: 'transcription_status', status: 'processing' })
      ),
    ];
    const clearable = Array.from({ length: 10 }, () => make({ readAgoMs: HOUR }));
    const out = prune([...clearable, ...protectedRows]).kept;
    expect(out).toHaveLength(protectedRows.length);
    expect(ids(out)).toEqual(ids(protectedRows));
  });
});

// A file's processing records render as ONE card (consolidated processing notification), so
// pruning them one at a time would leave a card claiming "transcription waiting" for a file
// whose transcription record simply expired first.
describe('a file processing card is retained as one unit', () => {
  const job = (type: NotificationType, over: Parameters<typeof make>[0] = {}) =>
    make({ type, status: 'completed', data: { file_id: 'f1', status: 'completed' }, ...over });

  it('keeps every record of a card while any one of them is still inside its window', () => {
    const tx = job('transcription_status', { readAgoMs: 30 * HOUR });
    const summary = job('summarization_status', { readAgoMs: HOUR });
    const out = prune([summary, tx]);
    expect(ids(out.kept)).toEqual([summary.id, tx.id]);
    expect(out.removedVisible).toBe(0);
  });

  it('drops the whole card at once and reports it as ONE cleared item', () => {
    const tx = job('transcription_status', { readAgoMs: 30 * HOUR });
    const summary = job('summarization_status', { readAgoMs: 25 * HOUR });
    const other = make({ readAgoMs: HOUR });
    const out = prune([summary, other, tx]);
    expect(ids(out.kept)).toEqual([other.id]);
    expect(out.removedVisible).toBe(1);
  });

  it('a failure anywhere in the card pins the whole card', () => {
    const tx = job('transcription_status', { readAgoMs: 30 * DAY });
    const summary = job('summarization_status', {
      status: 'error',
      data: { file_id: 'f1', status: 'failed' },
      readAgoMs: 30 * DAY,
    });
    expect(prune([summary, tx]).kept).toHaveLength(2);
  });

  it("a skipped LLM task (shown as nothing) does not pin the card, and redaction 'done' is a success", () => {
    const tx = job('transcription_status', { readAgoMs: 30 * HOUR });
    const topics = job('topic_extraction_status', {
      status: undefined,
      data: { file_id: 'f1', status: 'not_configured' },
      readAgoMs: 30 * HOUR,
    });
    const redaction = job('redaction_status', {
      status: undefined,
      data: { file_id: 'f1', status: 'done' },
      readAgoMs: 30 * HOUR,
    });
    expect(prune([redaction, topics, tx]).kept).toEqual([]);
    // Outside a card, not_configured is still a warning and is kept.
    const lone = make({
      type: 'auto_label_status',
      data: { status: 'not_configured' },
      readAgoMs: 30 * DAY,
    });
    expect(prune([lone]).kept).toEqual([lone]);
  });

  it('the cap evicts a card whole, counting its records toward the limit', () => {
    const tx = job('transcription_status', { readAgoMs: 2 * HOUR, ageMs: 9 * DAY });
    const summary = job('summarization_status', { readAgoMs: 2 * HOUR, ageMs: 9 * DAY });
    const filler = Array.from({ length: MAX_NOTIFICATIONS - 1 }, () => make({ readAgoMs: HOUR }));
    const out = prune([...filler, summary, tx]);
    // 101 records: dropping the oldest unit (the 2-record card) brings it to 99.
    expect(out.kept).toHaveLength(MAX_NOTIFICATIONS - 1);
    expect(ids(out.kept)).not.toContain(tx.id);
    expect(ids(out.kept)).not.toContain(summary.id);
    expect(out.removedVisible).toBe(1);
  });
});
