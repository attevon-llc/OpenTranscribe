/**
 * Paced reveal for the query-trace tree (GH #514).
 *
 * **The problem.** Frames arrive when the pipeline produces them, and on a
 * cached or fast turn all ~16 land inside ~200ms. Revealing each the instant it
 * arrives makes sixteen rows appear at once — a flicker, not an animation.
 *
 * **The fix is to decouple arrival from reveal.** Arrived nodes queue here; a
 * ticker promotes them on a schedule. That is the standard technique behind
 * every product whose progress list reads as a cascade rather than a pop.
 *
 * Pacing is *legibility*, not decoration: the sequence IS the information this
 * panel exists to convey, and a simultaneous dump destroys it. (An earlier
 * draft of the plan said "add no stagger"; that was wrong, and this supersedes
 * it.)
 *
 * Three properties keep it honest:
 *
 * - **Self-disabling on a slow turn.** Frames further apart than the interval
 *   find an empty buffer and reveal immediately. The mechanism costs nothing
 *   exactly when it is not needed.
 * - **It accelerates rather than queueing without bound.** A deep buffer
 *   shortens the interval, and `finish()` drains what is left. The animation is
 *   never the reason the trace lags the answer.
 * - **Siblings reveal together.** Nodes sharing a parent that arrive in the same
 *   batch are released as one group — they genuinely ran concurrently, and
 *   staggering them would misreport the one thing the issue calls the visual
 *   payoff.
 *
 * The clock is injected so tests are deterministic: timing logic tested through
 * a component with real timers is the slowest and flakiest kind of test there
 * is.
 */

/** One node waiting to be revealed. */
export interface PacedItem {
  key: string;
  /** Wire parent id, used to release genuine siblings together. */
  parent: string | null;
}

export interface PacerOptions {
  /** Milliseconds between reveals while the buffer is shallow. */
  intervalMs?: number;
  /** Buffer depth past which the interval shortens. */
  flushThreshold?: number;
  /** Reveal everything immediately — `prefers-reduced-motion`. */
  reducedMotion?: boolean;
  /** Injected clock; defaults to the real one. */
  now?: () => number;
}

const DEFAULT_INTERVAL_MS = 55;
const DEFAULT_FLUSH_THRESHOLD = 6;

/**
 * Decides *when* queued nodes become visible. Holds no timers itself — the
 * caller ticks it — so it stays pure and synchronously testable.
 */
export class RevealPacer {
  private readonly intervalMs: number;
  private readonly flushThreshold: number;
  private readonly reducedMotion: boolean;
  private readonly now: () => number;

  private queue: PacedItem[] = [];
  private revealed = new Set<string>();
  private lastReleaseAt: number | null = null;

  constructor(options: PacerOptions = {}) {
    this.intervalMs = options.intervalMs ?? DEFAULT_INTERVAL_MS;
    this.flushThreshold = options.flushThreshold ?? DEFAULT_FLUSH_THRESHOLD;
    this.reducedMotion = options.reducedMotion ?? false;
    this.now = options.now ?? (() => Date.now());
  }

  /** Keys revealed so far. The component renders exactly these. */
  get visible(): ReadonlySet<string> {
    return this.revealed;
  }

  get pending(): number {
    return this.queue.length;
  }

  /**
   * Queue nodes for reveal, skipping any already visible.
   *
   * Re-offering a revealed key is a no-op, which is what stops a closed-and-
   * reopened panel replaying the whole tree as if it were arriving now.
   */
  offer(items: PacedItem[]): void {
    for (const item of items) {
      if (this.revealed.has(item.key)) continue;
      if (this.queue.some((q) => q.key === item.key)) continue;
      this.queue.push(item);
    }
  }

  /**
   * Advance the pacer. Returns the keys revealed by THIS tick (possibly none).
   *
   * Under reduced motion, or once the buffer is deep, this releases more than
   * one node — see the class docstring.
   */
  tick(): string[] {
    if (!this.queue.length) return [];

    if (this.reducedMotion) return this.releaseAll();

    const now = this.now();
    if (this.lastReleaseAt !== null && now - this.lastReleaseAt < this.currentInterval()) {
      return [];
    }
    this.lastReleaseAt = now;
    return this.releaseGroup();
  }

  /** Reveal everything still queued — the turn is over; do not trail it. */
  finish(): string[] {
    return this.releaseAll();
  }

  /** Forget all state. Used when the panel switches to a different turn. */
  reset(): void {
    this.queue = [];
    this.revealed = new Set();
    this.lastReleaseAt = null;
  }

  /**
   * A deep buffer shortens the interval proportionally, so a burst catches up
   * instead of trailing the answer by seconds.
   */
  private currentInterval(): number {
    if (this.queue.length <= this.flushThreshold) return this.intervalMs;
    const factor = Math.ceil(this.queue.length / this.flushThreshold);
    return Math.max(1, Math.floor(this.intervalMs / factor));
  }

  /**
   * Release the head node plus every immediately-following node sharing its
   * parent: those are genuine concurrent siblings (a fan-out), and revealing
   * them one at a time would animate a sequence the pipeline never ran.
   */
  private releaseGroup(): string[] {
    const head = this.queue[0];
    const group: string[] = [];
    while (this.queue.length && this.queue[0].parent === head.parent) {
      const item = this.queue.shift() as PacedItem;
      this.revealed.add(item.key);
      group.push(item.key);
    }
    return group;
  }

  private releaseAll(): string[] {
    const keys = this.queue.map((item) => item.key);
    for (const key of keys) this.revealed.add(key);
    this.queue = [];
    this.lastReleaseAt = this.now();
    return keys;
  }
}
