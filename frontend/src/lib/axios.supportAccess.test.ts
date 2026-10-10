/**
 * Support-access behaviour of the shared axios instance (issue #1122), driven through a
 * real axios adapter so the interceptors' ordering is what is under test:
 *
 *  - the grant header is attached to tenant routes while a session is active, and never to
 *    the exempt lifecycle/auth/system routes;
 *  - a response carrying a SESSION-ENDING code ends the session with the matching reason,
 *    but only when the failed request actually carried the header;
 *  - a 403 without a code (an ordinary out-of-tenant refusal), and the three
 *    "this action is refused, the grant is fine" codes, never end the session.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { AxiosResponse, InternalAxiosRequestConfig } from 'axios';

vi.mock('$lib/edition', () => ({ isCloudEdition: false }));

const h = vi.hoisted(() => ({
  end: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock('$stores/supportSession', () => ({ supportSession: { end: h.end } }));
vi.mock('$stores/toast', () => ({ toastStore: { error: h.toastError } }));
vi.mock('$stores/locale', () => ({
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
}));

import axiosInstance from '$lib/axios';
import { setActiveSupportGrant } from '$lib/supportAccess/headers';

const GRANT = '11111111-2222-3333-4444-555555555555';

let seenHeaders: Record<string, unknown>[] = [];
let reply: { status: number; data: unknown } = { status: 200, data: {} };
const originalAdapter = axiosInstance.defaults.adapter;

function adapter(config: InternalAxiosRequestConfig): Promise<AxiosResponse> {
  seenHeaders.push(config.headers.toJSON() as Record<string, unknown>);
  const response = {
    status: reply.status,
    statusText: String(reply.status),
    data: reply.data,
    headers: {},
    config,
  } as AxiosResponse;
  if (reply.status >= 200 && reply.status < 300) return Promise.resolve(response);
  return Promise.reject(
    Object.assign(new Error(`status ${reply.status}`), { response, config, isAxiosError: true })
  );
}

function coded(status: number, code: string) {
  reply = { status, data: { detail: { code, message: 'm' } } };
}

async function call(url: string): Promise<void> {
  await axiosInstance.get(url).catch(() => undefined);
}

beforeEach(() => {
  vi.clearAllMocks();
  seenHeaders = [];
  reply = { status: 200, data: {} };
  axiosInstance.defaults.adapter = adapter as never;
  setActiveSupportGrant(null);
});

afterEach(() => {
  axiosInstance.defaults.adapter = originalAdapter;
  setActiveSupportGrant(null);
});

describe('grant header injection through the real interceptor', () => {
  it('attaches the header to a tenant route while a session is active', async () => {
    setActiveSupportGrant(GRANT);
    await call('/files/abc');
    expect(seenHeaders[0]['X-Support-Access-Grant']).toBe(GRANT);
  });

  it('does not attach it to /auth/me even while active', async () => {
    setActiveSupportGrant(GRANT);
    await call('/auth/me');
    expect(seenHeaders[0]['X-Support-Access-Grant']).toBeUndefined();
  });

  it('does not attach it when no session is active', async () => {
    await call('/files/abc');
    expect(seenHeaders[0]['X-Support-Access-Grant']).toBeUndefined();
  });
});

describe('response codes while the header was sent', () => {
  beforeEach(() => setActiveSupportGrant(GRANT));

  it.each([
    ['support_grant_revoked', 'revoked'],
    ['support_grant_expired', 'expired'],
    ['support_grant_not_active', 'invalid'],
    ['support_grant_invalid', 'invalid'],
    ['support_access_unavailable', 'invalid'],
  ])('%s ends the session as %s', async (code, reason) => {
    coded(code === 'support_access_unavailable' ? 400 : 403, code);
    await call('/files/abc');
    expect(h.end).toHaveBeenCalledTimes(1);
    expect(h.end).toHaveBeenCalledWith(reason);
  });

  it('a plain 403 with no code does not end the session', async () => {
    reply = { status: 403, data: { detail: 'Forbidden' } };
    await call('/files/abc');
    expect(h.end).not.toHaveBeenCalled();
    expect(h.toastError).not.toHaveBeenCalled();
  });

  it('a 404 for an out-of-tenant resource does not end the session', async () => {
    reply = { status: 404, data: { detail: 'Not found' } };
    await call('/files/abc');
    expect(h.end).not.toHaveBeenCalled();
  });

  it.each([
    ['support_grant_write_required', 403, 'supportAccess.error.readOnly'],
    ['support_grant_action_not_permitted', 403, 'supportAccess.error.notPermitted'],
    ['support_access_audit_unavailable', 503, 'supportAccess.error.auditUnavailable'],
  ])('%s toasts %s and keeps the session', async (code, status, key) => {
    coded(status, code);
    await call('/files/abc');
    expect(h.toastError).toHaveBeenCalledWith(key);
    expect(h.end).not.toHaveBeenCalled();
  });
});

describe('response codes when the header was NOT sent', () => {
  it('ignores a session-ending code from a request that carried no grant', async () => {
    // e.g. the exempt revoke call itself, or a request made after the session ended.
    coded(403, 'support_grant_revoked');
    await call('/files/abc');
    expect(h.end).not.toHaveBeenCalled();
  });

  it('an exempt route answering a code never ends a live session', async () => {
    setActiveSupportGrant(GRANT);
    coded(403, 'support_grant_revoked');
    await call('/support-access/grants/x/revoke');
    expect(h.end).not.toHaveBeenCalled();
  });
});
