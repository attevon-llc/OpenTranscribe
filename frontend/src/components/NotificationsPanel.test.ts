/**
 * `NotificationsPanel.svelte` and its children (`components/notifications/`).
 *
 * Icon coverage: the icon registry has shipped a silent-fallback-to-bell bug before
 * (`'video'`/`'music'` with no render branch), which only a rendering assertion catches.
 *
 * Processing cards: one file used to produce one row per event type (transcription, summary,
 * topics, redaction), each counted in the badge. The card tests below pin ONE card per file,
 * the weighted bar's LLM dependence as rendered, the details toggle's a11y wiring, the card's
 * dismiss/open acting on every record it holds, and a live region that speaks outcomes only.
 * The duration "trap" (processing time vs recording length) is pinned in
 * `$lib/notifications/processingJob.test.ts`, where durations are now computed.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { writable, get } from 'svelte/store';
import type { Notification, NotificationType } from '../stores/websocket';

vi.mock('../stores/auth', () => ({ token: writable(null) }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string, vars?: Record<string, unknown>) =>
        vars ? `${key}:${JSON.stringify(vars)}` : key
      );
      return () => {};
    },
  },
}));

// A minimal hand-rolled store rather than svelte/store's `writable`: `vi.mock`
// factories (and the `vi.hoisted` values they close over) run before this
// file's own imports are live, so `writable` isn't callable yet at this point.
const notificationsStore = vi.hoisted(() => {
  let value: { notifications: Notification[] } = { notifications: [] };
  const subscribers = new Set<(v: typeof value) => void>();
  return {
    subscribe(run: (v: typeof value) => void) {
      subscribers.add(run);
      run(value);
      return () => subscribers.delete(run);
    },
    set(next: typeof value) {
      value = next;
      subscribers.forEach((run) => run(value));
    },
    get: () => value,
  };
});
const removeNotification = vi.hoisted(() => vi.fn());
const markAsRead = vi.hoisted(() => vi.fn());
const markAllAsRead = vi.hoisted(() => vi.fn());
const clearAll = vi.hoisted(() => vi.fn());
const goto = vi.hoisted(() => vi.fn());
const llmAvailable = vi.hoisted(() => {
  let value = false;
  const subscribers = new Set<(v: boolean) => void>();
  return {
    subscribe(run: (v: boolean) => void) {
      subscribers.add(run);
      run(value);
      return () => subscribers.delete(run);
    },
    set(next: boolean) {
      value = next;
      subscribers.forEach((run) => run(value));
    },
  };
});

vi.mock('$app/navigation', () => ({ goto }));
vi.mock('$stores/llmStatus', () => ({ isLLMAvailable: { subscribe: llmAvailable.subscribe } }));

vi.mock('../stores/websocket', () => ({
  websocketStore: {
    subscribe: notificationsStore.subscribe,
    removeNotification,
    markAsRead,
    markAllAsRead,
    clearAll,
  },
}));

import { showNotificationsPanel } from '../stores/notificationsPanel';
import { clearedNotice, notificationRetention } from '../stores/notificationRetention';
import NotificationsPanel from './NotificationsPanel.svelte';

function baseNotification(overrides: Partial<Notification> = {}): Notification {
  return {
    id: overrides.id ?? 'n1',
    type: overrides.type ?? ('transcription_status' as NotificationType),
    title: overrides.title ?? 'Title',
    message: overrides.message ?? 'Message',
    timestamp: overrides.timestamp ?? new Date(),
    read: overrides.read ?? false,
    silent: false,
    ...overrides,
  } as Notification;
}

function setNotifications(list: Notification[]) {
  notificationsStore.set({ notifications: list });
}

/** The panel body loads on first open (bundle budget), so wait for that chunk. */
async function renderPanel() {
  const result = render(NotificationsPanel);
  await vi.dynamicImportSettled();
  await tick();
  return result;
}

function currentNotifications(): Notification[] {
  return notificationsStore.get().notifications;
}

// Transform the lazily loaded body once, up front: under a loaded full-suite run its first
// compile alone can exceed a single test's timeout.
beforeAll(async () => {
  await import('./notifications/NotificationsPanelBody.svelte');
}, 60_000);

beforeEach(() => {
  vi.clearAllMocks();
  setNotifications([]);
  llmAvailable.set(false);
  showNotificationsPanel.set(true);
});

const BELL_PATH = 'M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9';

describe('icon coverage — every mapped type renders its own glyph, not the bell fallback', () => {
  const cases: Array<[NotificationType, string]> = [
    ['topic_extraction_status', 'tag'],
    ['auto_label_status', 'tag'],
    ['redaction_status', 'eye-off'],
    ['youtube_processing_status', 'video'],
    ['playlist_processing_status', 'video'],
    ['audio_extraction_status', 'music'],
    ['collection_shared', 'folder'],
    ['group_member_added', 'users'],
    ['file_takedown', 'shield'],
    ['download_progress', 'download'],
  ];

  it.each(cases)('%s does not fall back to the generic bell glyph', async (type) => {
    setNotifications([baseNotification({ id: type, type })]);

    const { container } = await renderPanel();

    const icon = container.querySelector('.notification-icon svg');
    expect(icon?.innerHTML).not.toContain(BELL_PATH);
  });

  it('transcription_status renders the OpenTranscribe logo, not a generic glyph (item 14)', async () => {
    setNotifications([baseNotification({ id: 'tx', type: 'transcription_status' })]);

    const { container } = await renderPanel();

    const img = container.querySelector('.notification-icon img');
    expect(img).not.toBeNull();
    expect(img?.getAttribute('src')).toContain('logo-icon');
    // Decorative: the notification title carries the meaning.
    expect(img?.getAttribute('alt')).toBe('');
    expect(container.querySelector('.notification-icon svg')).toBeNull();
  });

  it('summarization_status keeps the file-text glyph (only transcription gets the logo)', async () => {
    setNotifications([baseNotification({ id: 'sm', type: 'summarization_status' })]);

    const { container } = await renderPanel();

    expect(container.querySelector('.notification-icon img')).toBeNull();
    expect(container.querySelector('.notification-icon svg path')?.getAttribute('d')).toContain(
      'M14 2H6a2'
    );
  });

  it('topic and tag notifications share one glyph, and collections use the folder glyph (item 14)', async () => {
    const glyph = async (type: NotificationType) => {
      setNotifications([baseNotification({ id: type, type })]);
      const { container, unmount } = await renderPanel();
      const html = container.querySelector('.notification-icon svg')?.innerHTML;
      unmount();
      return html;
    };
    expect(await glyph('topic_extraction_status')).toBe(await glyph('auto_label_status'));
    expect(await glyph('topic_extraction_status')).toContain('M20.59 13.41');
    expect(await glyph('collection_shared')).toContain('M22 19a2 2 0 0 1-2 2H4');
  });

  it('an unmapped type falls back to the bell on purpose (not a bug — most union members have no icon yet)', async () => {
    setNotifications([baseNotification({ id: 'x', type: 'echo' as NotificationType })]);

    const { container } = await renderPanel();

    const icon = container.querySelector('.notification-icon svg');
    expect(icon?.innerHTML).toContain(BELL_PATH);
  });
});

describe('getNotificationStatus fallback (issue #753 J3 / #569 N2)', () => {
  it('colours a row from top-level notification.status when data.status is absent', async () => {
    // This is exactly the shape `addNotification` callers (audio extraction,
    // the #569 download bridge) produce when they pass no `data` object.
    setNotifications([baseNotification({ status: 'completed' })]);

    const { container } = await renderPanel();

    const item = container.querySelector('.notification-item');
    expect(item?.className).toContain('status-success');
    // The pre-fix behavior was silent, not an error: without the fallback
    // this renders `status-default` (grey, uncoloured) instead — assert the
    // negative too, or a future regression back to `data.status`-only would
    // still pass on `not.toBeNull` finding some OTHER element in the DOM.
    expect(item?.className).not.toContain('status-default');
  });

  it('still prefers data.status when both are present and they disagree', async () => {
    setNotifications([baseNotification({ status: 'processing', data: { status: 'completed' } })]);

    const { container } = await renderPanel();

    expect(container.querySelector('.notification-item.status-success')).not.toBeNull();
    expect(container.querySelector('.notification-item.status-info')).toBeNull();
  });
});

describe('sanity: the store subscription this file depends on is exercised', () => {
  it('renders one row per notification and reflects clearAll', async () => {
    setNotifications([baseNotification({ id: 'a' }), baseNotification({ id: 'b' })]);

    const { container } = await renderPanel();

    expect(container.querySelectorAll('.notification-item')).toHaveLength(2);
    expect(currentNotifications()).toHaveLength(2);
  });
});

describe('retention controls (issue #1217)', () => {
  beforeEach(() => {
    localStorage.clear();
    notificationRetention.set('read24h');
    clearedNotice.reset();
  });

  it("offers the three choices, defaults to '24 hours after reading', and persists a change", async () => {
    const { container } = await renderPanel();

    const select = container.querySelector<HTMLSelectElement>('.retention-setting select');
    expect(select?.value).toBe('read24h');
    expect([...(select?.options ?? [])].map((o) => o.value)).toEqual([
      'read24h',
      'read7d',
      'never',
    ]);

    await fireEvent.change(select!, { target: { value: 'never' } });

    expect(get(notificationRetention)).toBe('never');
    expect(localStorage.getItem('opentr:notificationRetention')).toBe('never');
  });

  it('says nothing when no older notifications were cleared', async () => {
    const { container } = await renderPanel();

    expect(container.querySelector('.cleared-notice')?.textContent?.trim()).toBe('');
    expect(container.querySelector('.cleared-notice')?.getAttribute('aria-live')).toBe('polite');
  });

  it('announces how many were cleared, politely, and the dismiss control removes it', async () => {
    clearedNotice.add(3);
    showNotificationsPanel.set(false);
    showNotificationsPanel.set(true);

    const { container } = await renderPanel();

    const notice = container.querySelector('.cleared-notice');
    expect(notice?.textContent).toContain('notifications.clearedNotice');
    expect(notice?.textContent).toContain('"count":3');

    await fireEvent.click(notice!.querySelector('button')!);

    expect(container.querySelector('.cleared-notice')?.textContent?.trim()).toBe('');
    showNotificationsPanel.set(false);
    showNotificationsPanel.set(true);
    expect(get(clearedNotice)).toBe(0);
  });
});

describe('processing card: one card per file', () => {
  const NOW = Date.now();

  function fileRecord(type: NotificationType, status: string, over: Partial<Notification> = {}) {
    return baseNotification({
      id: `${type}-${status}`,
      type,
      status: status as Notification['status'],
      dismissible: status !== 'processing',
      timestamp: new Date(NOW),
      ...over,
      data: { file_id: 'f1', filename: 'meeting.mp3', status, ...(over.data ?? {}) },
    });
  }

  const txDone = (over: Partial<Notification> = {}) =>
    fileRecord('transcription_status', 'completed', {
      startedAt: NOW - 200_000,
      finishedAt: NOW - 1_000,
      progress: { current: 100, total: 100, percentage: 100 },
      ...over,
      data: { duration_seconds: 140, ...(over.data ?? {}) },
    });

  it('renders one card and one badge count for four events of one file', async () => {
    setNotifications([
      fileRecord('topic_extraction_status', 'processing'),
      fileRecord('summarization_status', 'processing'),
      fileRecord('redaction_status', 'done'),
      txDone(),
    ]);

    const { container } = await renderPanel();

    expect(container.querySelectorAll('.notification-item')).toHaveLength(1);
    expect(container.querySelectorAll('.processing-job')).toHaveLength(1);
    expect(container.querySelector('.unread-badge')?.textContent).toBe('1');
    expect(container.querySelector('.job-file')?.textContent).toBe('meeting.mp3');
  });

  /** The backend's post-processing run for f1: a plan at server time 100, then outcomes. */
  const postProcessing = (tasks: Record<string, 'queued' | 'started' | 'done' | 'skipped'>) =>
    baseNotification({
      id: 'post_processing_status-f1',
      type: 'post_processing_status',
      progressId: 'post_processing_status_f1',
      status: Object.values(tasks).some((s) => s === 'queued' || s === 'started')
        ? 'processing'
        : 'completed',
      dismissible: true,
      timestamp: new Date(NOW),
      data: { file_id: 'f1' },
      postProcessing: {
        runAt: 100,
        tasks: Object.fromEntries(
          Object.entries(tasks).map(([k, state]) => [k, { state, at: 101 }])
        ),
        updatedAt: NOW,
      },
    });

  const ALL_FINISHED = {
    search_indexing: 'done',
    file_facts: 'done',
    analytics: 'done',
    redaction: 'skipped',
    speaker_attributes: 'done',
    speaker_clustering: 'done',
    summary: 'skipped',
    topics: 'skipped',
  } as const;

  it('a finished transcript with post-processing still running reads "Finishing up"', async () => {
    setNotifications([
      postProcessing({ ...ALL_FINISHED, speaker_attributes: 'started' }),
      txDone(),
    ]);

    const { container } = await renderPanel();

    expect(container.querySelector('.job-state')?.textContent).toBe(
      'notifications.job.stateFinishing'
    );
    expect(container.querySelector('.job-detail')?.textContent).toContain(
      'notifications.job.task.speaker_attributes'
    );
    const bar = container.querySelector('[role="progressbar"]');
    expect(Number(bar?.getAttribute('aria-valuenow'))).toBeLessThan(100);
    expect(bar?.getAttribute('aria-label')).toContain('meeting.mp3');
    // The finished main tier collapses to a chip; the running task is listed.
    const tierNames = [...container.querySelectorAll('.tier-chips .visually-hidden')].map(
      (c) => c.textContent
    );
    expect(tierNames.some((n) => n?.includes('notifications.job.tier.main'))).toBe(true);
    expect(container.querySelectorAll('.task-chips .job-chip')).toHaveLength(1);
  });

  it('the bar reaches 100% and "Complete" only when every known task is finished', async () => {
    setNotifications([postProcessing(ALL_FINISHED), txDone()]);

    const { container } = await renderPanel();

    expect(container.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe(
      '100'
    );
    expect(container.querySelector('.job-state')?.textContent).toBe(
      'notifications.job.stateComplete'
    );
    // No LLM tasks ran, so there is no AI tier and no task row.
    expect(container.textContent).not.toContain('notifications.job.tier.ai');
    expect(container.querySelector('.task-chips')).toBeNull();
  });

  it('with an LLM configured the summary and topic chips wait and the bar holds at 67%', async () => {
    llmAvailable.set(true);
    setNotifications([txDone()]);

    const { container } = await renderPanel();

    expect(container.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe(
      '67'
    );
    const chipNames = [...container.querySelectorAll('.job-chip .visually-hidden')].map(
      (c) => c.textContent
    );
    expect(
      chipNames.some(
        (n) =>
          n?.includes('notifications.job.task.summary') &&
          n.includes('notifications.job.state.pending')
      )
    ).toBe(true);

    // Without an LLM the AI share goes, but the finishing tier still holds the bar back
    // until the backend's plan says what will run: 60 / (60 + 15).
    llmAvailable.set(false);
    await tick();
    expect(container.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe(
      '80'
    );
    expect(container.querySelector('.job-detail')?.textContent).toBe('notifications.job.preparing');
  });

  it('the details toggle is wired for assistive tech and lists task durations', async () => {
    setNotifications([txDone()]);

    const { container } = await renderPanel();
    const toggle = container.querySelector<HTMLButtonElement>('.job-toggle')!;

    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(container.querySelector('.job-breakdown')).toBeNull();

    await fireEvent.click(toggle);

    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const region = container.querySelector('.job-breakdown');
    expect(region?.id).toBe(toggle.getAttribute('aria-controls'));
    expect(region?.textContent).toContain('notifications.job.task.transcription');
    expect(region?.textContent).toContain('2m 20s');
  });

  it('cannot be dismissed while any task runs; once finished, dismiss removes every record', async () => {
    const summary = fileRecord('summarization_status', 'processing');
    setNotifications([summary, txDone()]);
    const { container } = await renderPanel();

    expect(container.querySelector<HTMLButtonElement>('.notification-dismiss')?.disabled).toBe(
      true
    );

    setNotifications([fileRecord('summarization_status', 'completed'), txDone()]);
    await tick();
    await fireEvent.click(container.querySelector('.notification-dismiss')!);

    expect(removeNotification.mock.calls.map((c) => c[0]).sort()).toEqual(
      ['summarization_status-completed', 'transcription_status-completed'].sort()
    );
  });

  it('opening the file marks every unread record read, navigates and closes the panel', async () => {
    setNotifications([fileRecord('summarization_status', 'completed'), txDone({ read: true })]);
    const { container } = await renderPanel();

    await fireEvent.click(container.querySelector('.job-file')!);

    expect(markAsRead.mock.calls.map((c) => c[0])).toEqual(['summarization_status-completed']);
    expect(goto).toHaveBeenCalledWith('/files/f1');
    expect(get(showNotificationsPanel)).toBe(false);
  });

  it('announces the outcome once, politely, and never a progress tick', async () => {
    const running = (p: number) =>
      fileRecord('transcription_status', 'processing', {
        startedAt: NOW - 10_000,
        progress: { current: p, total: 100, percentage: p },
      });
    setNotifications([running(10)]);
    const { container } = await renderPanel();
    const region = container.querySelector('.job-announcer');

    expect(region?.getAttribute('aria-live')).toBe('polite');
    expect(region?.textContent).toBe('');

    setNotifications([running(60)]);
    await tick();
    expect(region?.textContent).toBe('');

    // A finished transcript is not the outcome: post-processing still follows, unannounced.
    setNotifications([txDone()]);
    await tick();
    expect(region?.textContent).toBe('');

    setNotifications([postProcessing(ALL_FINISHED), txDone()]);
    await tick();
    expect(region?.textContent).toContain('notifications.job.announceDone');
    expect(region?.textContent).toContain('meeting.mp3');
  });

  it('does not announce a card that was already finished when the panel opened', async () => {
    setNotifications([postProcessing(ALL_FINISHED), txDone()]);
    const { container } = await renderPanel();
    expect(container.querySelector('.job-announcer')?.textContent).toBe('');
  });
});

describe('bundle: the card model stays out of the root layout', () => {
  // The panel shell is mounted by routes/+layout.svelte and the websocket store is imported
  // by it too, so anything they import statically is paid on every page load (bundle budget
  // gate, frontend/CLAUDE.md). The card model and its components load on first open.
  const read = (rel: string) => readFileSync(join(__dirname, rel), 'utf8');
  const STATIC_IMPORT = /^\s*import\s+(?!type\b)[^;]*?from\s+['"]([^'"]+)['"]/gm;
  const staticImports = (source: string) => [...source.matchAll(STATIC_IMPORT)].map((m) => m[1]);

  it('the shell loads its body with a dynamic import and imports no card code statically', () => {
    const shell = read('NotificationsPanel.svelte');
    expect(shell).toContain("import('./notifications/NotificationsPanelBody.svelte')");
    expect(staticImports(shell).filter((s) => s.includes('notifications/'))).toEqual([]);
  });

  it('the websocket store and retention import only the small grouping module', () => {
    for (const rel of ['../stores/websocket.ts', '../lib/notifications/retention.ts']) {
      const imports = staticImports(read(rel));
      expect(imports.some((s) => s.endsWith('jobGrouping'))).toBe(true);
      expect(imports.filter((s) => s.endsWith('processingJob') || s.endsWith('display'))).toEqual(
        []
      );
    }
  });
});
