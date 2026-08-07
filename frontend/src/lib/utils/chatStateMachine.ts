/**
 * Stream lifecycle rules for chat (issue #52).
 *
 * Kept as a pure module, separate from the store, for two reasons: the store
 * imports SvelteKit navigation (untestable in isolation), and these rules are
 * the thing most worth testing directly. A late or duplicated SSE frame must
 * never reopen a finished stream and leave the composer stuck showing "Stop".
 */

import type { StreamStatus } from '$lib/types/chat';

/** States a stream cannot leave except via a fresh send. */
const TERMINAL: readonly StreamStatus[] = ['done', 'error', 'aborted'];

/** Forward-only progression through an active send. */
const PROGRESSION: readonly StreamStatus[] = [
  'idle',
  'submitting',
  'retrieving',
  'thinking',
  'streaming',
];

/**
 * Whether a status change should be applied.
 *
 * @param from - Current stream status.
 * @param to - Status implied by an incoming event.
 * @returns True when the transition is legal; false means the event is stale
 *   and should be ignored.
 */
export function canTransition(from: StreamStatus, to: StreamStatus): boolean {
  if (from === to) return true;

  if (TERMINAL.includes(from)) {
    // Only a fresh send (or a reset) may leave a terminal state.
    return to === 'submitting' || to === 'idle';
  }

  // Interruption and failure are reachable from any phase that actually began.
  if (TERMINAL.includes(to)) return from !== 'idle';

  const fromIndex = PROGRESSION.indexOf(from);
  const toIndex = PROGRESSION.indexOf(to);
  if (fromIndex === -1 || toIndex === -1) return false;

  // Never move backwards: a 'retrieving' status arriving after the first token
  // is stale noise, not a reason to hide the streaming answer.
  return toIndex >= fromIndex;
}
