/**
 * `sessionSurvivesRevocation` decides whether a socket the server closed with 4004
 * (sessions revoked) may reconnect. It asks the HTTP client, whose 401 path either
 * refreshes the session or ends it; the auth state afterwards is the answer.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const auth = vi.hoisted(() => ({ isAuthenticated: null as unknown }));
const http = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  auth.isAuthenticated = writable(true);
  return { isAuthenticated: auth.isAuthenticated };
});
vi.mock('$lib/axios', () => ({ default: { get: http.get } }));

import type { Writable } from 'svelte/store';
import { sessionSurvivesRevocation, SESSION_REVOKED_CLOSE_CODE } from './sessionRecheck';

const signedIn = () => auth.isAuthenticated as Writable<boolean>;

beforeEach(() => {
  http.get.mockReset();
  signedIn().set(true);
});

describe('sessionSurvivesRevocation', () => {
  it('matches the backend close code for a revoked session', () => {
    expect(SESSION_REVOKED_CLOSE_CODE).toBe(4004);
  });

  it('asks through a route the HTTP client refreshes on', async () => {
    http.get.mockResolvedValue({ data: {} });

    await sessionSurvivesRevocation();

    expect(http.get.mock.calls).toEqual([['/users/me']]);
  });

  it('is true when the request succeeds and the user is still signed in', async () => {
    http.get.mockResolvedValue({ data: {} });

    expect(await sessionSurvivesRevocation()).toBe(true);
  });

  it('is false once the refresh-failure path has ended the session', async () => {
    http.get.mockImplementation(async () => {
      signedIn().set(false);
      throw new Error('refresh failed');
    });

    expect(await sessionSurvivesRevocation()).toBe(false);
  });

  it('is true after a network failure that left the session alone', async () => {
    http.get.mockRejectedValue(new Error('Network Error'));

    expect(await sessionSurvivesRevocation()).toBe(true);
  });
});
