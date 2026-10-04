/**
 * The idle / absolute session guard (issue #1106).
 *
 * Fake timers drive the clock; activity is dispatched as real DOM events on a
 * private EventTarget so nothing leaks between tests. The cross-tab channel is an
 * in-memory BroadcastChannel stand-in that delivers to every OTHER member, which is
 * what the real one does.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createIdleGuard,
  ACTIVITY_EVENTS,
  type BroadcastLike,
  type IdleGuard,
  type IdleGuardState,
} from './idleGuard';

const MIN = 60_000;

class FakeBus {
  members: FakeChannel[] = [];
  create(): FakeChannel {
    const ch = new FakeChannel(this);
    this.members.push(ch);
    return ch;
  }
}

class FakeChannel implements BroadcastLike {
  onmessage: ((ev: MessageEvent) => void) | null = null;
  posted: unknown[] = [];
  closed = false;
  constructor(private bus: FakeBus) {}
  postMessage(data: unknown): void {
    this.posted.push(data);
    for (const other of this.bus.members) {
      if (other !== this && !other.closed) other.onmessage?.({ data } as MessageEvent);
    }
  }
  close(): void {
    this.closed = true;
  }
}

interface Harness {
  guard: IdleGuard;
  target: EventTarget;
  doc: { visibilityState: string } & EventTarget;
  states: IdleGuardState[];
  last: () => IdleGuardState;
}

const guards: IdleGuard[] = [];

function makeDoc(): { visibilityState: string } & EventTarget {
  const doc = new EventTarget() as { visibilityState: string } & EventTarget;
  doc.visibilityState = 'visible';
  return doc;
}

function start(
  opts: Partial<Parameters<typeof createIdleGuard>[0]> = {},
  channel: BroadcastLike | null = null
): Harness {
  const target = new EventTarget();
  const doc = makeDoc();
  const states: IdleGuardState[] = [];
  const guard = createIdleGuard({
    idleTimeoutMs: 15 * MIN,
    absoluteDeadline: null,
    warningLeadMs: 2 * MIN,
    target,
    doc,
    channel,
    onChange: (s) => states.push(s),
    ...opts,
  });
  guards.push(guard);
  return { guard, target, doc, states, last: () => guard.getState() };
}

function input(target: EventTarget, type = 'pointerdown'): void {
  target.dispatchEvent(new Event(type));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-09-30T12:00:00Z'));
});

afterEach(() => {
  while (guards.length) guards.pop()!.stop();
  vi.useRealTimers();
});

describe('idle timeout', () => {
  it('stays active before the warning window', () => {
    const h = start();
    vi.advanceTimersByTime(12 * MIN);
    expect(h.last()).toEqual({ phase: 'active' });
  });

  it('warns N minutes before the timeout, then expires', () => {
    const h = start();
    vi.advanceTimersByTime(13 * MIN + 1000);
    expect(h.last()).toMatchObject({ phase: 'warning', reason: 'idle' });

    vi.advanceTimersByTime(2 * MIN);
    expect(h.last()).toEqual({ phase: 'expired', reason: 'idle' });
  });

  it('reports each transition once, in order', () => {
    const h = start();
    vi.advanceTimersByTime(16 * MIN);
    expect(h.states.map((s) => s.phase)).toEqual(['warning', 'expired']);
  });

  it.each(ACTIVITY_EVENTS)('real input (%s) resets the clock', (type) => {
    const h = start();
    vi.advanceTimersByTime(10 * MIN);
    input(h.target, type);
    vi.advanceTimersByTime(10 * MIN);
    expect(h.last()).toEqual({ phase: 'active' });
  });

  it('input during the warning dismisses it', () => {
    const h = start();
    vi.advanceTimersByTime(14 * MIN);
    expect(h.last().phase).toBe('warning');
    input(h.target, 'keydown');
    expect(h.last()).toEqual({ phase: 'active' });
  });

  it('"stay signed in" dismisses the warning and restarts the clock', () => {
    const h = start();
    vi.advanceTimersByTime(14 * MIN);
    h.guard.staySignedIn();
    expect(h.last()).toEqual({ phase: 'active' });
    vi.advanceTimersByTime(12 * MIN);
    expect(h.last()).toEqual({ phase: 'active' });
  });

  it('background requests and token refresh do not count as activity', async () => {
    const h = start();
    vi.advanceTimersByTime(10 * MIN);
    // What background traffic looks like to the page: network events, timers and
    // focus-less DOM churn. None of them is user input.
    for (const type of ['load', 'progress', 'message', 'online', 'focus', 'storage']) {
      input(h.target, type);
    }
    await Promise.resolve();
    vi.advanceTimersByTime(6 * MIN);
    expect(h.last()).toEqual({ phase: 'expired', reason: 'idle' });
  });

  it('input after expiry does not resurrect the session', () => {
    const h = start();
    vi.advanceTimersByTime(16 * MIN);
    input(h.target);
    vi.advanceTimersByTime(1000);
    expect(h.last()).toEqual({ phase: 'expired', reason: 'idle' });
  });

  it('a zero idle timeout disables the idle half', () => {
    const h = start({ idleTimeoutMs: 0 });
    vi.advanceTimersByTime(24 * 60 * MIN);
    expect(h.last()).toEqual({ phase: 'active' });
  });
});

describe('suspend / resume', () => {
  it('returning to a tab after the deadline expires it instead of counting as activity', () => {
    const h = start();
    h.doc.visibilityState = 'hidden';
    h.doc.dispatchEvent(new Event('visibilitychange'));
    // The machine sleeps: wall-clock time moves, timers do not fire.
    vi.setSystemTime(Date.now() + 60 * MIN);
    h.doc.visibilityState = 'visible';
    h.doc.dispatchEvent(new Event('visibilitychange'));
    expect(h.last()).toEqual({ phase: 'expired', reason: 'idle' });
  });

  it('returning to a tab before the deadline counts as activity', () => {
    const h = start();
    vi.advanceTimersByTime(10 * MIN);
    h.doc.visibilityState = 'visible';
    h.doc.dispatchEvent(new Event('visibilitychange'));
    vi.advanceTimersByTime(10 * MIN);
    expect(h.last()).toEqual({ phase: 'active' });
  });
});

describe('absolute timeout', () => {
  it('warns and expires at the absolute deadline regardless of activity', () => {
    const h = start({ absoluteDeadline: Date.now() + 30 * MIN });
    for (let i = 0; i < 28; i++) {
      vi.advanceTimersByTime(MIN);
      input(h.target);
    }
    expect(h.last()).toMatchObject({ phase: 'warning', reason: 'absolute' });
    input(h.target);
    expect(h.last()).toMatchObject({ phase: 'warning', reason: 'absolute' });
    vi.advanceTimersByTime(2 * MIN);
    expect(h.last()).toEqual({ phase: 'expired', reason: 'absolute' });
  });

  it('"stay signed in" cannot extend the absolute deadline', () => {
    const h = start({ absoluteDeadline: Date.now() + 5 * MIN });
    vi.advanceTimersByTime(4 * MIN);
    h.guard.staySignedIn();
    vi.advanceTimersByTime(MIN + 1000);
    expect(h.last()).toEqual({ phase: 'expired', reason: 'absolute' });
  });

  it('an absolute deadline already in the past expires immediately', () => {
    const h = start({ absoluteDeadline: Date.now() - 1 });
    expect(h.last()).toEqual({ phase: 'expired', reason: 'absolute' });
  });

  it('whichever deadline comes first wins', () => {
    const h = start({ idleTimeoutMs: 15 * MIN, absoluteDeadline: Date.now() + 10 * MIN });
    vi.advanceTimersByTime(10 * MIN);
    expect(h.last()).toEqual({ phase: 'expired', reason: 'absolute' });
  });
});

describe('cross-tab', () => {
  it('input in one tab keeps the other tab alive', () => {
    const bus = new FakeBus();
    const a = start({}, bus.create());
    const b = start({}, bus.create());
    for (let i = 0; i < 4; i++) {
      vi.advanceTimersByTime(5 * MIN);
      input(b.target);
    }
    expect(a.last()).toEqual({ phase: 'active' });
    expect(b.last()).toEqual({ phase: 'active' });
  });

  it('"stay signed in" in one tab dismisses the warning in the other', () => {
    const bus = new FakeBus();
    const a = start({}, bus.create());
    const b = start({}, bus.create());
    vi.advanceTimersByTime(14 * MIN);
    expect(a.last().phase).toBe('warning');
    b.guard.staySignedIn();
    expect(a.last()).toEqual({ phase: 'active' });
  });

  it('expiry in one tab expires every tab', () => {
    const bus = new FakeBus();
    const a = start({}, bus.create());
    const b = start({ idleTimeoutMs: 60 * MIN }, bus.create());
    vi.advanceTimersByTime(16 * MIN);
    expect(a.last()).toEqual({ phase: 'expired', reason: 'idle' });
    expect(b.last()).toEqual({ phase: 'expired', reason: 'idle' });
  });

  it('ignores malformed channel messages', () => {
    const bus = new FakeBus();
    const a = start({}, bus.create());
    const other = bus.create();
    other.postMessage({ type: 'activity', at: 'soon' });
    other.postMessage({ type: 'expired', reason: 'because' });
    other.postMessage('noise');
    expect(a.last()).toEqual({ phase: 'active' });
  });

  it('closes its channel and stops listening when stopped', () => {
    const bus = new FakeBus();
    const ch = bus.create();
    const h = start({}, ch);
    h.guard.stop();
    vi.advanceTimersByTime(30 * MIN);
    expect(ch.closed).toBe(true);
    expect(h.last()).toEqual({ phase: 'active' });
  });
});
