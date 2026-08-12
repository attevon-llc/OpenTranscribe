/**
 * LOGOUT TEARDOWN ORDER.
 *
 * DEFECT THESE CATCH: `logout()` called `clearUserState()` and only then
 * `authStore.reset()`, and the WebSocket closed **only as a side effect** of
 * `authStore.token` going null (`websocket.ts` subscribes to it). Between those
 * two statements the socket was still open and still handling frames, and every
 * notification handler calls `saveNotificationsToStorage()` — so one inbound
 * frame arriving during teardown re-populated `state.notifications` AND re-wrote
 * `localStorage['notifications']` *after* `clearUserState()` had deleted the key.
 * The previous user's notifications then survived on disk into the next session.
 *
 * `logout()` now closes the socket explicitly, first.
 *
 * The `FakeWebSocket` double refuses to deliver on a closed socket, exactly like
 * a browser — see the calibration test at the bottom, which proves delivery
 * really does work while the socket is open (otherwise every assertion here
 * would pass vacuously).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/axios', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ status: 200, data: {} }),
    post: vi.fn().mockResolvedValue({ status: 200, data: {} }),
    put: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  },
  abortAllRequests: vi.fn(),
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$lib/edition', () => ({ isCloudEdition: false }));

import { FakeWebSocket, installFakeWebSocket, restoreWebSocket } from '../test-mocks/fakeWebSocket';
import { websocketStore } from './websocket';
import { authStore, logout } from './auth';

/** A real backend frame that lands in the notification panel and on disk. */
const TRANSCRIPTION_FRAME = {
  type: 'transcription_status',
  data: { file_id: 'file-of-user-a', status: 'completed', message: 'Transcription complete' },
};

function connectOpenSocket(): FakeWebSocket {
  websocketStore.connect();
  const socket = FakeWebSocket.latest();
  socket.simulateOpen();
  return socket;
}

describe('logout teardown order', () => {
  beforeEach(() => {
    installFakeWebSocket();
    localStorage.clear();
    websocketStore.clearAll();
    authStore.setToken('cookie');
  });

  afterEach(() => {
    restoreWebSocket();
    localStorage.clear();
  });

  it('closes the WebSocket before clearUserState erases session state', async () => {
    const socket = connectOpenSocket();
    localStorage.setItem('notifications', JSON.stringify([{ id: 'a', title: 'User A' }]));

    // Observe the socket's state at the exact moment the session's localStorage
    // keys are erased. The subject of the assertion is the socket's readyState —
    // real state, not a call count.
    // NOTE: the hook MUST go on Storage.prototype. jsdom's `localStorage` is a
    // Proxy whose `set` writes a storage ITEM, so `localStorage.removeItem = fn`
    // silently stores a key called "removeItem" and never intercepts anything —
    // a hook that looks installed and is not.
    let socketStateWhenKeyErased: number | null = null;
    const realRemoveItem = Storage.prototype.removeItem;
    const hook = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (
      this: Storage,
      key: string
    ) {
      if (key === 'notifications' && socketStateWhenKeyErased === null) {
        socketStateWhenKeyErased = socket.readyState;
      }
      realRemoveItem.call(this, key);
    });

    try {
      await logout();
    } finally {
      hook.mockRestore();
    }

    expect(socketStateWhenKeyErased).toBe(FakeWebSocket.CLOSED);
    expect(socket.closedWith?.[0]).toBe(1000);
  });

  it('a frame arriving mid-logout cannot rewrite localStorage[notifications]', async () => {
    const socket = connectOpenSocket();

    // Fire the frame from inside the teardown window: this callback runs while
    // clearUserState() is midway through its localStorage pass, which is where
    // the leak used to happen.
    let deliveredDuringTeardown: boolean | null = null;
    const realRemoveItem = Storage.prototype.removeItem;
    const hook = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (
      this: Storage,
      key: string
    ) {
      realRemoveItem.call(this, key);
      if (key === 'notifications' && deliveredDuringTeardown === null) {
        deliveredDuringTeardown = socket.deliver(TRANSCRIPTION_FRAME);
      }
    });

    try {
      await logout();
    } finally {
      hook.mockRestore();
    }

    // The socket was already closed, so the frame was never handled...
    expect(deliveredDuringTeardown).toBe(false);
    // ...and nothing wrote the key back after it was deleted.
    expect(localStorage.getItem('notifications')).toBeNull();
    expect(get(websocketStore).notifications).toEqual([]);
  });

  it('leaves no notifications in the store or on disk after logout', async () => {
    const socket = connectOpenSocket();
    expect(socket.deliver(TRANSCRIPTION_FRAME)).toBe(true);
    expect(get(websocketStore).notifications.length).toBeGreaterThan(0);
    expect(JSON.parse(localStorage.getItem('notifications') ?? '[]').length).toBeGreaterThan(0);

    await logout();

    expect(get(websocketStore).notifications).toEqual([]);
    expect(localStorage.getItem('notifications')).toBeNull();
    expect(get(authStore).isAuthenticated).toBe(false);
  });

  it('CALIBRATION: an open socket really does persist a frame to localStorage', () => {
    // Without this, the three tests above could all pass because `deliver()` is
    // broken rather than because the socket is closed. A double that can never
    // deliver makes every leak test unfalsifiable.
    const socket = connectOpenSocket();

    expect(socket.deliver(TRANSCRIPTION_FRAME)).toBe(true);

    const persisted = JSON.parse(localStorage.getItem('notifications') ?? '[]');
    expect(persisted.length).toBeGreaterThan(0);
    expect(get(websocketStore).notifications[0].data?.file_id).toBe('file-of-user-a');
  });
});
