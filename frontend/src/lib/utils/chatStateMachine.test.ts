/**
 * Chat store state machine (issue #52).
 *
 * The transition rules are what keep the composer honest: a late or duplicated
 * SSE frame must never reopen a finished stream and leave the send button stuck
 * on "Stop".
 */

import { describe, expect, it } from 'vitest';

import { canTransition } from './chatStateMachine';
import type { StreamStatus } from '$lib/types/chat';

const TERMINAL: StreamStatus[] = ['done', 'error', 'aborted'];

describe('canTransition — happy path', () => {
  it('walks the normal send lifecycle', () => {
    expect(canTransition('idle', 'submitting')).toBe(true);
    expect(canTransition('submitting', 'retrieving')).toBe(true);
    expect(canTransition('retrieving', 'thinking')).toBe(true);
    expect(canTransition('thinking', 'streaming')).toBe(true);
    expect(canTransition('streaming', 'done')).toBe(true);
  });

  it('allows skipping retrieval (context-off conversations)', () => {
    expect(canTransition('submitting', 'thinking')).toBe(true);
    expect(canTransition('submitting', 'streaming')).toBe(true);
  });

  it('treats a repeated status as a no-op rather than an error', () => {
    expect(canTransition('streaming', 'streaming')).toBe(true);
    expect(canTransition('retrieving', 'retrieving')).toBe(true);
  });
});

describe('canTransition — late and out-of-order frames', () => {
  it('never moves backwards once tokens have started', () => {
    // A 'retrieving' status arriving after the first delta is stale noise.
    expect(canTransition('streaming', 'retrieving')).toBe(false);
    expect(canTransition('thinking', 'submitting')).toBe(false);
    expect(canTransition('streaming', 'thinking')).toBe(false);
  });

  it('does not let a stray frame reopen a finished stream', () => {
    for (const terminal of TERMINAL) {
      expect(canTransition(terminal, 'streaming')).toBe(false);
      expect(canTransition(terminal, 'retrieving')).toBe(false);
      expect(canTransition(terminal, 'thinking')).toBe(false);
    }
  });

  it('lets a finished stream be replaced by a new send', () => {
    for (const terminal of TERMINAL) {
      expect(canTransition(terminal, 'submitting')).toBe(true);
      expect(canTransition(terminal, 'idle')).toBe(true);
    }
  });
});

describe('canTransition — interruption', () => {
  it('allows aborting from any active phase', () => {
    for (const from of ['submitting', 'retrieving', 'thinking', 'streaming'] as StreamStatus[]) {
      expect(canTransition(from, 'aborted')).toBe(true);
    }
  });

  it('allows failing from any active phase', () => {
    for (const from of ['submitting', 'retrieving', 'thinking', 'streaming'] as StreamStatus[]) {
      expect(canTransition(from, 'error')).toBe(true);
    }
  });

  it('cannot abort or fail a stream that never started', () => {
    expect(canTransition('idle', 'aborted')).toBe(false);
    expect(canTransition('idle', 'error')).toBe(false);
    expect(canTransition('idle', 'done')).toBe(false);
  });
});
