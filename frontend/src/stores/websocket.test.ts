/**
 * Tests for `$stores/websocket` — the app's realtime plane.
 *
 * DEFECT THESE CATCH: 0 of 481 lines and 0 of 81 functions were covered. This is
 * the module through which EVERY live status update reaches the UI, and its
 * dispatch is one long `if (data.type === '...')` chain keyed on string literals
 * shared with the backend. Rename a message type on either side and:
 *   - no import breaks, no type error appears, `npm run check` stays green;
 *   - files sit at "processing" forever, because the notification that would have
 *     flipped them lands in the generic `else` branch as an unrecognised toast;
 *   - the `apiCache.invalidateByScope` fan-out stops firing, so a stale file list
 *     survives its full 5-minute TTL after a change the user just made.
 *
 * Also pinned here: reconnect policy (a clean 1000/1001 close must NOT reconnect,
 * an abnormal 1006 must, and neither may happen while the page is hidden), the
 * progressive-notification reducer, and the 100-notification cap.
 *
 * The `FakeWebSocket` double models close/deliver honestly — `deliver()` returns
 * false on a closed socket — so "the frame was ignored" is a real observation.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/edition', () => ({ isCloudEdition: false }));

vi.mock('$lib/axios', () => ({
  default: {
    // `recoverActiveProgress()` runs on every open; give it an empty task list.
    get: vi.fn().mockResolvedValue({ data: [] }),
    post: vi.fn().mockResolvedValue({ status: 200, data: {} }),
  },
  abortAllRequests: vi.fn(),
}));

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return {
    t: readable((key: string, opts?: Record<string, unknown>) =>
      opts ? `${key}:${JSON.stringify(opts)}` : key
    ),
  };
});

import { FakeWebSocket, installFakeWebSocket, restoreWebSocket } from '../test-mocks/fakeWebSocket';
import { websocketStore, unreadCount, connectionStatus } from './websocket';
import { apiCache, CacheTTL } from '$lib/apiCache';

function openSocket(): FakeWebSocket {
  websocketStore.connect();
  const socket = FakeWebSocket.latest();
  socket.simulateOpen();
  return socket;
}

function notifications() {
  return get(websocketStore).notifications;
}

beforeEach(() => {
  installFakeWebSocket();
  vi.useFakeTimers();
  localStorage.clear();
  websocketStore.clearAll();
  apiCache.clear();
});

afterEach(() => {
  vi.useRealTimers();
  restoreWebSocket();
  localStorage.clear();
});

// ─────────────────────────────────────────────────────────────────────────────
// Connection lifecycle
// ─────────────────────────────────────────────────────────────────────────────

describe('websocket connection lifecycle', () => {
  it('connects to /api/ws on the current origin, upgrading scheme from the page', () => {
    const socket = openSocket();

    expect(socket.url).toBe(`ws://${window.location.host}/api/ws`);
    expect(get(websocketStore).status).toBe('connected');
  });

  it('closing and reconnecting replaces the socket rather than stacking handlers', () => {
    const first = openSocket();
    const second = openSocket();

    expect(second).not.toBe(first);
    expect(first.readyState).toBe(FakeWebSocket.CLOSED);
    // The old socket's handlers are detached first, so its close cannot trigger
    // a reconnect that races the new one.
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('does NOT reconnect after a clean close (code 1000) — that is a logout', () => {
    const socket = openSocket();

    socket.simulateServerClose(1000, 'User logged out');
    vi.advanceTimersByTime(60_000);

    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(get(websocketStore).reconnectAttempts).toBe(0);
  });

  it('does NOT reconnect after code 1001 (page going away)', () => {
    const socket = openSocket();

    socket.simulateServerClose(1001, 'going away');
    vi.advanceTimersByTime(60_000);

    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('DOES reconnect after an abnormal close (1006), with a backoff delay', () => {
    const socket = openSocket();

    socket.simulateServerClose(1006, 'abnormal');
    expect(get(websocketStore).reconnectAttempts).toBe(1);
    // Not immediate: reconnecting in the same tick would hammer a restarting
    // backend with one client per dropped connection.
    expect(FakeWebSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances.length).toBeGreaterThan(1);
  });

  it('backs off further on each successive failure', () => {
    openSocket();
    FakeWebSocket.latest().simulateServerClose(1006);
    expect(get(websocketStore).reconnectAttempts).toBe(1);

    vi.advanceTimersByTime(60_000);
    FakeWebSocket.latest().simulateServerClose(1006);

    // Attempts accumulate — that is what feeds reconnectDelayMs's exponent. A
    // counter that reset each time would make the backoff a fixed delay.
    expect(get(websocketStore).reconnectAttempts).toBeGreaterThan(1);
  });

  it('resets the attempt counter once a connection succeeds', () => {
    openSocket();
    FakeWebSocket.latest().simulateServerClose(1006);
    expect(get(websocketStore).reconnectAttempts).toBe(1);

    vi.advanceTimersByTime(60_000);
    FakeWebSocket.latest().simulateOpen();

    expect(get(websocketStore).reconnectAttempts).toBe(0);
  });

  it('does not reconnect while the page is hidden', () => {
    const socket = openSocket();
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);

    socket.simulateServerClose(1006);
    vi.advanceTimersByTime(60_000);

    expect(FakeWebSocket.instances).toHaveLength(1);
    hidden.mockRestore();
  });

  it('surfaces an error status without dropping the socket', () => {
    const socket = openSocket();

    socket.simulateError();

    expect(get(websocketStore).status).toBe('error');
    expect(get(websocketStore).error).toBeTruthy();
  });

  it('disconnect() closes cleanly with 1000 so no reconnect is scheduled', () => {
    const socket = openSocket();

    websocketStore.disconnect();
    vi.advanceTimersByTime(60_000);

    expect(socket.closedWith?.[0]).toBe(1000);
    expect(get(websocketStore).status).toBe('disconnected');
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

describe('connectionStatus (UI-facing)', () => {
  it('reports connected while the socket is open', () => {
    openSocket();
    expect(get(connectionStatus)).toBe('connected');
  });

  it('reports reconnecting during backoff, not disconnected', () => {
    // The distinction drives whether the UI shows "lost connection" or a spinner.
    const socket = openSocket();
    socket.simulateServerClose(1006);

    expect(get(connectionStatus)).toBe('reconnecting');
  });

  it('reports disconnected after a clean close', () => {
    const socket = openSocket();
    socket.simulateServerClose(1000);

    expect(get(connectionStatus)).toBe('disconnected');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Message dispatch
// ─────────────────────────────────────────────────────────────────────────────

describe('websocket message dispatch', () => {
  it('ignores connection_established and echo without creating a notification', () => {
    const socket = openSocket();

    socket.deliver({ type: 'connection_established', data: {} });
    socket.deliver({ type: 'echo', data: {} });

    expect(notifications()).toEqual([]);
  });

  it('survives a malformed frame instead of tearing down the socket', () => {
    const socket = openSocket();
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});

    socket.onmessage?.({ data: 'not json' });

    expect(get(websocketStore).status).toBe('connected');
    expect(spy).toHaveBeenCalled();
    // And the socket still works afterwards.
    socket.deliver({ type: 'file_takedown', data: { file_id: 'f1', filename: 'a.mp4' } });
    expect(notifications()).toHaveLength(1);
  });

  it.each([
    ['transcription_status', 'transcription_status_f1'],
    ['summarization_status', 'summarization_status_f1'],
    ['redaction_status', 'redaction_status_f1'],
    ['topic_extraction_status', 'topic_extraction_status_f1'],
    ['auto_label_status', 'auto_label_status_f1'],
  ])('%s creates ONE progressive notification keyed by progressId', (type, progressId) => {
    const socket = openSocket();

    socket.deliver({ type, data: { file_id: 'f1', status: 'processing', progress: 25 } });
    socket.deliver({ type, data: { file_id: 'f1', status: 'processing', progress: 60 } });

    // Two frames, one notification — a renamed type would produce two generic
    // toasts instead, and the file would never leave "processing".
    const ns = notifications().filter((n) => n.progressId === progressId);
    expect(ns).toHaveLength(1);
    expect(ns[0].progress?.percentage).toBe(60);
    expect(ns[0].status).toBe('processing');
  });

  it('keeps a progressive notification non-dismissible until it finishes', () => {
    const socket = openSocket();

    socket.deliver({
      type: 'transcription_status',
      data: { file_id: 'f1', status: 'processing', progress: 10 },
    });
    expect(notifications()[0].dismissible).toBe(false);

    socket.deliver({
      type: 'transcription_status',
      data: { file_id: 'f1', status: 'completed', progress: 100 },
    });
    expect(notifications()[0].dismissible).toBe(true);
    expect(notifications()[0].status).toBe('completed');
  });

  it('moves an updated progressive notification back to the front', () => {
    const socket = openSocket();

    socket.deliver({
      type: 'transcription_status',
      data: { file_id: 'first', status: 'processing', progress: 5 },
    });
    socket.deliver({
      type: 'transcription_status',
      data: { file_id: 'second', status: 'processing', progress: 5 },
    });
    expect(notifications()[0].progressId).toBe('transcription_status_second');

    socket.deliver({
      type: 'transcription_status',
      data: { file_id: 'first', status: 'processing', progress: 50 },
    });

    expect(notifications()[0].progressId).toBe('transcription_status_first');
    expect(notifications()).toHaveLength(2);
  });

  it('normalises admin progress from 0-1 while leaving file progress on 0-100', () => {
    const socket = openSocket();

    // Admin tasks send a fraction...
    socket.deliver({
      type: 'reindex_progress',
      data: { progress: 0.4, indexed_files: 4, total_files: 10 },
    });
    expect(
      notifications().find((n) => n.progressId === 'admin_reindex')?.progress?.percentage
    ).toBe(40);

    // ...file tasks send a percentage. Applying the admin ×100 to these would
    // report 2500%.
    socket.deliver({ type: 'transcription_status', data: { file_id: 'f1', progress: 25 } });
    expect(
      notifications().find((n) => n.progressId === 'transcription_status_f1')?.progress?.percentage
    ).toBe(25);
  });

  it('maps every admin task family onto its shared progressId', () => {
    const socket = openSocket();

    socket.deliver({ type: 'reindex_progress', data: { progress: 0.1 } });
    socket.deliver({ type: 'reindex_complete', data: { progress: 1 } });

    // Progress and completion must collapse into ONE row, not two.
    expect(notifications().filter((n) => n.progressId === 'admin_reindex')).toHaveLength(1);
    expect(notifications().find((n) => n.progressId === 'admin_reindex')?.status).toBe('completed');
  });

  it('renders an ETA when the backend sends eta_seconds', () => {
    const socket = openSocket();

    socket.deliver({
      type: 'transcription_status',
      data: { file_id: 'f1', status: 'processing', progress: 50, eta_seconds: 125 },
    });

    const n = notifications()[0];
    expect(n.progress?.etaSeconds).toBe(125);
    expect(n.progress?.etaDisplay).toBe('2m 5s');
  });

  it('omits the ETA display for a null or non-positive eta_seconds', () => {
    const socket = openSocket();

    socket.deliver({
      type: 'transcription_status',
      data: { file_id: 'f1', progress: 50, eta_seconds: null },
    });
    expect(notifications()[0].progress?.etaDisplay).toBeUndefined();

    socket.deliver({
      type: 'transcription_status',
      data: { file_id: 'f2', progress: 50, eta_seconds: 0 },
    });
    expect(notifications()[0].progress?.etaDisplay).toBeUndefined();
  });

  it('marks gallery-only updates silent and keeps them out of localStorage', () => {
    const socket = openSocket();

    socket.deliver({ type: 'file_created', data: { file_id: 'f1', filename: 'a.mp4' } });

    expect(notifications()[0].silent).toBe(true);
    // Silent rows are noise on disk — they must not survive a reload.
    expect(JSON.parse(localStorage.getItem('notifications') ?? '[]')).toEqual([]);
    // ...and they must not inflate the unread badge.
    expect(get(unreadCount)).toBe(0);
  });

  it('counts a non-silent unread notification in the badge', () => {
    const socket = openSocket();

    socket.deliver({ type: 'file_takedown', data: { file_id: 'f1', filename: 'a.mp4' } });

    expect(get(unreadCount)).toBe(1);
  });

  it('caps the notification list at 100', () => {
    const socket = openSocket();

    for (let i = 0; i < 120; i += 1) {
      socket.deliver({ type: 'file_takedown', data: { file_id: `f${i}`, filename: 'a.mp4' } });
    }

    expect(notifications()).toHaveLength(100);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// apiCache fan-out
// ─────────────────────────────────────────────────────────────────────────────

describe('websocket → apiCache invalidation fan-out', () => {
  async function seedCache() {
    await apiCache.getOrFetch('collections:all', async () => ['stale'], CacheTTL.FILES);
    await apiCache.getOrFetch('files:page:1:', async () => ['stale'], CacheTTL.FILES);
    expect(apiCache.stats().size).toBe(2);
  }

  it('cache_invalidate drops the named scope', async () => {
    await seedCache();
    const socket = openSocket();

    socket.deliver({ type: 'cache_invalidate', data: { scope: 'files' } });
    await vi.waitFor(() => expect(apiCache.stats().size).toBe(1));
  });

  it.each([
    'collection_shared',
    'collection_share_revoked',
    'group_member_added',
    'group_member_removed',
  ])('%s invalidates the collections cache', async (type) => {
    // These are the events where ANOTHER user changed what this user can see. If
    // the fan-out stops firing the gallery shows a stale library for the full TTL.
    await seedCache();
    const socket = openSocket();

    socket.deliver({ type, data: { collection_id: 'c1' } });

    await vi.waitFor(() => expect(apiCache.stats().size).toBeLessThan(2));
  });

  it('file_takedown invalidates the files cache and still notifies the owner', async () => {
    await seedCache();
    const socket = openSocket();

    socket.deliver({ type: 'file_takedown', data: { file_id: 'f1', filename: 'a.mp4' } });

    await vi.waitFor(() => expect(apiCache.stats().size).toBeLessThan(2));
    // A takedown notice must be visible, not merely a cache hint.
    expect(notifications()).toHaveLength(1);
    expect(notifications()[0].silent).toBeUndefined();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// DOM events other components listen for
// ─────────────────────────────────────────────────────────────────────────────

describe('websocket → window CustomEvents', () => {
  it.each([
    ['gpu_stats_update', 'gpu-stats-updated'],
    ['speaker_updated', 'speaker-updated'],
    ['speaker_processing_complete', 'speaker-processing-complete'],
    ['clustering_progress', 'clustering-progress'],
    ['reindex_progress', 'reindex-progress'],
    ['migration_progress', 'migration-progress'],
    ['data_integrity_progress', 'data-integrity-progress'],
  ])('%s dispatches the %s window event', (messageType, eventName) => {
    // These event NAMES are a second, undeclared contract: several pages listen
    // for them by string. Renaming one silently stops a settings panel updating.
    const socket = openSocket();
    const handler = vi.fn();
    window.addEventListener(eventName, handler);

    socket.deliver({ type: messageType, data: { some: 'payload' } });

    expect(handler).toHaveBeenCalledTimes(1);
    window.removeEventListener(eventName, handler);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Notification management API
// ─────────────────────────────────────────────────────────────────────────────

describe('notification management', () => {
  it('markAsRead marks a completed notification read and persists it', () => {
    const socket = openSocket();
    socket.deliver({
      type: 'transcription_status',
      data: { file_id: 'f1', status: 'completed', progress: 100 },
    });
    const id = notifications()[0].id;

    websocketStore.markAsRead(id);

    expect(notifications()[0].read).toBe(true);
    const persisted = JSON.parse(localStorage.getItem('notifications') ?? '[]');
    expect(persisted[0].read).toBe(true);
  });

  it('markAsRead does NOT dismiss a still-processing notification', () => {
    // A processing row is re-marked unread on purpose: dismissing live progress
    // would hide the only indication that work is under way.
    const socket = openSocket();
    socket.deliver({
      type: 'transcription_status',
      data: { file_id: 'f1', status: 'processing', progress: 10 },
    });
    const id = notifications()[0].id;

    websocketStore.markAsRead(id);
    vi.advanceTimersByTime(200);

    expect(notifications()[0].read).toBe(false);
  });

  it('markAllAsRead zeroes the unread badge', () => {
    const socket = openSocket();
    socket.deliver({ type: 'file_takedown', data: { file_id: 'f1', filename: 'a.mp4' } });
    socket.deliver({ type: 'file_takedown_released', data: { file_id: 'f2', filename: 'b.mp4' } });
    expect(get(unreadCount)).toBe(2);

    websocketStore.markAllAsRead();

    expect(get(unreadCount)).toBe(0);
  });

  it('clearAll empties the store AND the persisted copy', () => {
    const socket = openSocket();
    socket.deliver({ type: 'file_takedown', data: { file_id: 'f1', filename: 'a.mp4' } });

    websocketStore.clearAll();

    expect(notifications()).toEqual([]);
    expect(JSON.parse(localStorage.getItem('notifications') ?? '[]')).toEqual([]);
  });

  it('removeNotification drops one row and leaves the rest', () => {
    const socket = openSocket();
    socket.deliver({ type: 'file_takedown', data: { file_id: 'f1', filename: 'a.mp4' } });
    socket.deliver({ type: 'file_takedown', data: { file_id: 'f2', filename: 'b.mp4' } });
    const id = notifications()[0].id;

    websocketStore.removeNotification(id);

    expect(notifications()).toHaveLength(1);
    expect(notifications()[0].id).not.toBe(id);
  });

  it('send() is a no-op when the socket is not connected, rather than throwing', () => {
    const spy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    websocketStore.disconnect();

    expect(() => websocketStore.send({ type: 'ping' })).not.toThrow();
    expect(spy).toHaveBeenCalled();
  });

  it('send() writes to an open socket', () => {
    const socket = openSocket();

    websocketStore.send({ type: 'ping' });

    expect(socket.sent).toContain(JSON.stringify({ type: 'ping' }));
  });
});
