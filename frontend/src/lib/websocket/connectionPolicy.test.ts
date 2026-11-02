/**
 * Pins the realtime reconnect policy. Close codes are a contract with
 * `backend/app/api/websockets.py`: 4004 means the user's sessions were revoked
 * (logout-all, password reset, admin termination), so the client must re-check the
 * session before reconnecting instead of retrying with a refused cookie. 4003 (bad or
 * expired token at the handshake) currently takes the ordinary backoff path; pinned so a
 * change to it is deliberate.
 */
import { describe, it, expect } from 'vitest';
import { closeAction, uiConnectionStatus, websocketUrl } from './connectionPolicy';

describe('websocketUrl', () => {
  it('uses wss on https and ws otherwise, on the page host', () => {
    expect(websocketUrl({ protocol: 'https:', host: 'ot.example:8443' })).toBe(
      'wss://ot.example:8443/api/ws'
    );
    expect(websocketUrl({ protocol: 'http:', host: '192.168.1.5:5173' })).toBe(
      'ws://192.168.1.5:5173/api/ws'
    );
  });
});

describe('closeAction', () => {
  it('re-checks the session on a revocation close, even while hidden', () => {
    expect(closeAction(4004, false)).toBe('recheck-session');
    expect(closeAction(4004, true)).toBe('recheck-session');
  });

  it('stays closed after a clean close', () => {
    expect(closeAction(1000, false)).toBe('stay-closed');
    expect(closeAction(1001, false)).toBe('stay-closed');
  });

  it('reconnects after an abnormal close while visible, not while hidden', () => {
    expect(closeAction(1006, false)).toBe('reconnect');
    expect(closeAction(1011, false)).toBe('reconnect');
    expect(closeAction(1006, true)).toBe('stay-closed');
  });

  it('treats a 4003 handshake refusal like any abnormal close', () => {
    expect(closeAction(4003, false)).toBe('reconnect');
  });
});

describe('uiConnectionStatus', () => {
  const socket = {} as WebSocket;
  it('maps the internal state to what the banner shows', () => {
    expect(uiConnectionStatus({ status: 'connected', reconnectAttempts: 3, socket })).toBe(
      'connected'
    );
    expect(uiConnectionStatus({ status: 'disconnected', reconnectAttempts: 1, socket: null })).toBe(
      'reconnecting'
    );
    expect(uiConnectionStatus({ status: 'connecting', reconnectAttempts: 0, socket })).toBe(
      'connecting'
    );
    expect(uiConnectionStatus({ status: 'error', reconnectAttempts: 0, socket })).toBe(
      'connecting'
    );
    expect(uiConnectionStatus({ status: 'error', reconnectAttempts: 0, socket: null })).toBe(
      'disconnected'
    );
    expect(uiConnectionStatus({ status: 'disconnected', reconnectAttempts: 0, socket: null })).toBe(
      'disconnected'
    );
  });
});
