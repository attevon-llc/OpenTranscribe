/**
 * A minimal, browser-faithful `WebSocket` double for jsdom.
 *
 * jsdom ships no WebSocket, which is one reason `$stores/websocket` (481 lines,
 * 81 functions) had zero test coverage. This models the parts of the contract
 * the store depends on, and — importantly — models them HONESTLY:
 *
 *  - `close()` moves to CLOSED and fires `onclose` with the code it was given,
 *    so the store's "don't reconnect on 1000/1001" branch is exercised for real.
 *  - `deliver()` is a NO-OP once closed, exactly like the browser. That is what
 *    makes "a frame that arrives after logout" a meaningful assertion instead of
 *    a tautology — if the socket is still open, the frame really is handled.
 *
 * Install with `installFakeWebSocket()` in `beforeEach` and `restore()` after.
 */

type Handler = ((ev: unknown) => void) | null;

export class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  /** Every socket constructed since the last `installFakeWebSocket()`. */
  static instances: FakeWebSocket[] = [];

  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSING = 2;
  readonly CLOSED = 3;

  readyState = FakeWebSocket.CONNECTING;
  onopen: Handler = null;
  onclose: Handler = null;
  onmessage: Handler = null;
  onerror: Handler = null;

  /** Frames the app sent (e.g. the cloud `authenticate` first-message). */
  readonly sent: string[] = [];
  /** `[code, reason]` of the close the app requested, if any. */
  closedWith: [number, string] | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(code = 1000, reason = ''): void {
    if (this.readyState === FakeWebSocket.CLOSED) return;
    this.readyState = FakeWebSocket.CLOSED;
    this.closedWith = [code, reason];
    this.onclose?.({ code, reason, wasClean: code === 1000 });
  }

  // ── test drivers (not part of the WebSocket API) ──

  /** Complete the handshake. */
  simulateOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({});
  }

  /**
   * Deliver a server frame. Returns `false` (and does nothing) when the socket
   * is not OPEN — the browser cannot deliver on a closed socket either.
   */
  deliver(payload: unknown): boolean {
    if (this.readyState !== FakeWebSocket.OPEN) return false;
    this.onmessage?.({ data: JSON.stringify(payload) });
    return true;
  }

  /** Server-side / network close (non-clean by default, so reconnect applies). */
  simulateServerClose(code = 1006, reason = 'abnormal'): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code, reason, wasClean: false });
  }

  simulateError(): void {
    this.onerror?.({});
  }

  static latest(): FakeWebSocket {
    const socket = FakeWebSocket.instances.at(-1);
    if (!socket) throw new Error('No FakeWebSocket was constructed');
    return socket;
  }
}

let original: unknown;

export function installFakeWebSocket(): void {
  FakeWebSocket.instances = [];
  original = (globalThis as Record<string, unknown>).WebSocket;
  (globalThis as Record<string, unknown>).WebSocket = FakeWebSocket;
}

export function restoreWebSocket(): void {
  (globalThis as Record<string, unknown>).WebSocket = original;
  FakeWebSocket.instances = [];
}
