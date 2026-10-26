/**
 * The live-updates banner across real sign-in / sign-out transitions.
 *
 * DEFECT THIS CATCHES: the banner showed "Reconnecting to live updates…" for the first
 * connect of every session (sign-in, page load), before anything had failed, and kept
 * saying it for a fresh sign-in when the previous session had been signed out during a
 * reconnect backoff. The banner is driven here through `authStore.setToken`, the same
 * subscription the app uses to open and close the socket, not by poking the store.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';

vi.mock('$lib/edition', () => ({ isCloudEdition: false }));

vi.mock('$lib/axios', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: [] }),
    post: vi.fn().mockResolvedValue({ status: 200, data: {} }),
  },
  abortAllRequests: vi.fn(),
}));

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((key: string) => key) };
});

import {
  FakeWebSocket,
  installFakeWebSocket,
  restoreWebSocket,
} from '../../test-mocks/fakeWebSocket';
import { authStore } from '$stores/auth';
import ConnectionStatusBanner from './ConnectionStatusBanner.svelte';

async function signIn(): Promise<FakeWebSocket> {
  authStore.setToken('cookie');
  await tick();
  return FakeWebSocket.latest();
}

async function signOut(): Promise<void> {
  authStore.setToken(null);
  await tick();
}

function bannerText(): string | null {
  return screen.queryByRole('status')?.textContent?.trim() ?? null;
}

beforeEach(async () => {
  installFakeWebSocket();
  vi.useFakeTimers();
  await signOut();
});

afterEach(async () => {
  await signOut();
  vi.useRealTimers();
  restoreWebSocket();
});

describe('ConnectionStatusBanner across sign-in', () => {
  it('stays hidden while the first connect of a session is in flight', async () => {
    render(ConnectionStatusBanner);

    const socket = await signIn();
    expect(socket.readyState).toBe(FakeWebSocket.CONNECTING);
    expect(bannerText()).toBeNull();

    socket.simulateOpen();
    await tick();
    expect(bannerText()).toBeNull();
  });

  it('says reconnecting as soon as that first connect fails', async () => {
    render(ConnectionStatusBanner);

    const socket = await signIn();
    // What a refused handshake looks like to the browser: an abnormal close, no reason.
    socket.simulateServerClose(1006);
    await tick();

    expect(bannerText()).toBe('connection.reconnecting');
  });

  it('starts the next session clean after a sign-out during reconnect backoff', async () => {
    render(ConnectionStatusBanner);

    const first = await signIn();
    first.simulateOpen();
    first.simulateServerClose(1006);
    await tick();
    expect(bannerText()).toBe('connection.reconnecting');

    await signOut();
    const second = await signIn();

    expect(second).not.toBe(first);
    expect(second.readyState).toBe(FakeWebSocket.CONNECTING);
    expect(bannerText()).toBeNull();
  });
});
