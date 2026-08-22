/**
 * The anti-flicker pacer (GH #514).
 *
 * Every test drives an injected clock rather than real timers: timing logic
 * tested through real waits is the slowest and flakiest kind of test there is,
 * and none of these assertions is about wall-clock duration anyway — they are
 * about *what is released when*.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { RevealPacer } from './revealPacer';

const INTERVAL = 55;

let clock = 0;
const now = () => clock;
const advance = (ms: number) => {
  clock += ms;
};

const pacer = (over: Partial<ConstructorParameters<typeof RevealPacer>[0]> = {}) =>
  new RevealPacer({ intervalMs: INTERVAL, now, ...over });

const items = (count: number, parent: string | null = 'turn') =>
  Array.from({ length: count }, (_, i) => ({ key: `n${i}`, parent }));

beforeEach(() => {
  clock = 0;
});

describe('RevealPacer', () => {
  it('reveals a burst one group at a time instead of all at once', () => {
    // The whole point. Sixteen frames arriving together must not become sixteen
    // rows appearing together, which reads as a flicker rather than a sequence.
    const p = pacer();
    p.offer(items(16).map((item, i) => ({ ...item, parent: `p${i}` })));

    const first = p.tick();

    expect(first).toHaveLength(1);
    expect(p.pending).toBe(15);
  });

  it('reveals immediately on a slow turn, so pacing costs nothing when idle', () => {
    // Frames further apart than the interval must never be held back: the
    // mechanism has to disable itself exactly when it is not needed.
    const p = pacer();

    p.offer([{ key: 'a', parent: 'turn' }]);
    expect(p.tick()).toEqual(['a']);

    advance(INTERVAL * 4);
    p.offer([{ key: 'b', parent: 'turn' }]);
    expect(p.tick()).toEqual(['b']);
  });

  it('holds a node back until the interval has elapsed', () => {
    // The control for the test above: without this, "reveals immediately" is
    // satisfied by a pacer that never paces at all.
    const p = pacer();
    p.offer([
      { key: 'a', parent: 'p1' },
      { key: 'b', parent: 'p2' },
    ]);

    expect(p.tick()).toEqual(['a']);
    advance(INTERVAL - 1);
    expect(p.tick()).toEqual([]);

    advance(2);
    expect(p.tick()).toEqual(['b']);
  });

  it('releases genuine siblings together rather than staggering them', () => {
    // A fan-out's legs ran concurrently. Revealing them one by one would
    // animate a sequence the pipeline never executed — the exact thing the
    // issue calls the visual payoff.
    const p = pacer();
    p.offer([
      { key: 'main', parent: 'plan' },
      { key: 'subquestion-0', parent: 'plan' },
      { key: 'counted', parent: 'plan' },
      { key: 'rerank', parent: 'turn' },
    ]);

    expect(p.tick()).toEqual(['main', 'subquestion-0', 'counted']);
    advance(INTERVAL);
    expect(p.tick()).toEqual(['rerank']);
  });

  it('accelerates once the buffer is deep so the trace cannot trail the answer', () => {
    const shallow = pacer();
    shallow.offer(items(2).map((item, i) => ({ ...item, parent: `p${i}` })));
    shallow.tick();

    const deep = pacer();
    deep.offer(items(40).map((item, i) => ({ ...item, parent: `p${i}` })));
    deep.tick();

    // Same interval setting, but the deep buffer must release again sooner.
    advance(10);
    expect(shallow.tick()).toEqual([]);
    expect(deep.tick()).not.toEqual([]);
  });

  it('reveals everything immediately under reduced motion', () => {
    // Reduced motion must bypass the pacer entirely: it is plain JS, so the
    // global CSS rule that collapses animation durations does not reach it.
    const p = pacer({ reducedMotion: true });
    p.offer(items(16).map((item, i) => ({ ...item, parent: `p${i}` })));

    const revealed = p.tick();

    expect(revealed).toHaveLength(16);
    expect(p.pending).toBe(0);
  });

  it('drains whatever is left when the turn finishes', () => {
    const p = pacer();
    p.offer(items(5).map((item, i) => ({ ...item, parent: `p${i}` })));
    p.tick();

    const rest = p.finish();

    expect(p.pending).toBe(0);
    expect(rest.length).toBeGreaterThan(0);
    expect(p.visible.size).toBe(5);
  });

  it('never replays a node that has already been revealed', () => {
    // Closing and reopening the panel re-offers every node. Replaying the whole
    // tree as if it were arriving now looks broken.
    const p = pacer();
    p.offer([{ key: 'a', parent: 'turn' }]);
    expect(p.tick()).toEqual(['a']);

    advance(INTERVAL * 10);
    p.offer([{ key: 'a', parent: 'turn' }]);

    expect(p.pending).toBe(0);
    expect(p.tick()).toEqual([]);
  });

  it('ignores a duplicate offer of a node already queued', () => {
    const p = pacer();
    p.offer([{ key: 'a', parent: 'turn' }]);
    p.offer([{ key: 'a', parent: 'turn' }]);

    expect(p.pending).toBe(1);
  });

  it('forgets everything on reset, so a new turn starts clean', () => {
    const p = pacer();
    p.offer(items(3));
    p.tick();
    expect(p.visible.size).toBeGreaterThan(0);

    p.reset();

    expect(p.pending).toBe(0);
    expect(p.visible.size).toBe(0);
  });

  it('does nothing when there is nothing queued', () => {
    const p = pacer();
    expect(p.tick()).toEqual([]);
    expect(p.finish()).toEqual([]);
  });
});
