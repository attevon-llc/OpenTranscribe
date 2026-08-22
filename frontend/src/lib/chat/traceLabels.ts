/**
 * Label and format helpers for the query-trace panel (GH #514).
 *
 * Pure and separately testable, so `ChatTraceNode` stays declarative and the
 * mapping rules are not buried in markup.
 *
 * Every lookup degrades to the raw machine code rather than a dotted key. The
 * backend's `reason` vocabulary can grow without a client release, and rendering
 * `chat.trace.reason.quota_guard` to a user would be worse than rendering
 * `quota_guard`.
 */

import type { TraceDetail, TraceOutcome, TraceStage } from '$lib/types/chat';
import type { TraceNode } from './traceTree';

export const stageLabelKey = (stage: TraceStage): string => `chat.trace.stage.${stage}`;
export const outcomeLabelKey = (outcome: TraceOutcome): string => `chat.trace.outcome.${outcome}`;
export const reasonLabelKey = (reason: string): string => `chat.trace.reason.${reason}`;

/**
 * The data source a node touched — the panel's whole point is showing WHERE a
 * query went, so `plane` (which index) wins over `source` (which system) when
 * both are present.
 *
 * There is deliberately no `document` plane: `_widen_to_document_plane` ORs the
 * document plane into the same chunk query rather than running a separate leg,
 * so labelling one would misreport what ran.
 */
export function sourceLabelKey(detail: TraceDetail): string | null {
  if (detail.plane) return `chat.trace.plane.${detail.plane}`;
  if (detail.source) return `chat.trace.source.${detail.source}`;
  return null;
}

/**
 * A duration, in the largest unit that stays readable.
 *
 * Sub-millisecond work reports as `0ms` rather than being hidden: a stage that
 * took no measurable time still RAN, and blanking it would make it look skipped.
 */
export function formatMs(
  ms: number | undefined
): { key: string; params: Record<string, number> } | null {
  if (ms === undefined || ms === null || Number.isNaN(ms)) return null;
  if (ms >= 1000) {
    return { key: 'chat.trace.timing.s', params: { s: Math.round(ms / 100) / 10 } };
  }
  return { key: 'chat.trace.timing.ms', params: { ms: Math.round(ms) } };
}

export interface DetailChip {
  key: string;
  params: Record<string, number>;
}

/**
 * The counts worth showing on a node, in reading order.
 *
 * ⚠️ An `empty` outcome renders an explicit `0`, and that is the point rather
 * than a quirk: "we looked and found nothing" must be visibly different from
 * "we never looked", and a blank count reads as the latter. A `skipped` node
 * shows no count at all, because there is genuinely nothing to count.
 */
export function detailChips(node: TraceNode): DetailChip[] {
  const chips: DetailChip[] = [];
  const { detail, outcome } = node;

  if (outcome === 'skipped') return chips;

  if (detail.kept !== undefined && detail.dropped !== undefined) {
    chips.push({
      key: 'chat.trace.detail.keptOfDropped',
      params: { kept: detail.kept, dropped: detail.dropped },
    });
  } else if (detail.kept !== undefined) {
    chips.push({ key: 'chat.trace.detail.kept', params: { kept: detail.kept } });
  } else if (detail.dropped !== undefined) {
    chips.push({ key: 'chat.trace.detail.dropped', params: { dropped: detail.dropped } });
  }

  if (detail.count !== undefined) {
    chips.push({ key: 'chat.trace.detail.found', params: { count: detail.count } });
  }
  if (detail.legs !== undefined) {
    chips.push({ key: 'chat.trace.detail.legs', params: { count: detail.legs } });
  }
  // `limit` is a configured bound, but WHICH bound depends on the stage, and
  // one shared label was wrong: the excerpt budget is a character count, so
  // rendering it as "max 92096/file" invented a per-file cap that does not
  // exist. Only label a limit where its meaning is known.
  if (detail.limit !== undefined) {
    if (node.stage === 'sampled') {
      chips.push({ key: 'chat.trace.detail.perFile', params: { limit: detail.limit } });
    } else if (node.stage === 'budgeted') {
      chips.push({ key: 'chat.trace.detail.budgetChars', params: { limit: detail.limit } });
    }
  }
  return chips;
}

/**
 * Marker shape per outcome, as a class suffix.
 *
 * Colour is reinforcement only. `empty` and `skipped` differ by marker FAMILY
 * (a hollow ring versus a dash) rather than by fill, because a fill difference
 * is what low-vision and colourblind users miss — and those two are exactly the
 * pair this feature exists to separate.
 */
export const OUTCOME_MARKERS: Record<TraceOutcome, string> = {
  ok: 'dot',
  empty: 'ring',
  skipped: 'dash',
  cached: 'square',
  declined: 'slash',
  failed: 'cross',
};

/**
 * Node ids that need to be shown, because the stage label alone is ambiguous.
 *
 * `FILTERED` fires twice per turn — quarantine and masking — and two rows both
 * reading "Filtered" with different numbers looks like a bug rather than two
 * different filters. Anything not listed here is already unambiguous from its
 * stage, and labelling every node would just add noise.
 */
const SUBJECT_NODES = new Set(['quarantine', 'masking']);

export const subjectLabelKey = (nodeId: string | null): string | null =>
  nodeId && SUBJECT_NODES.has(nodeId) ? `chat.trace.node.${nodeId}` : null;

export const markerClass = (outcome: TraceOutcome): string =>
  `trace-marker--${OUTCOME_MARKERS[outcome] ?? 'dot'}`;
