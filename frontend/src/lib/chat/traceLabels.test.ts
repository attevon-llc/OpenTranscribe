/**
 * Label and chip mapping for the query-trace panel (GH #514).
 *
 * ⚠️ Every assertion here is on a translation KEY, never on rendered English.
 * vitest loads no locale bundle, so `$t` returns the raw key — an assertion on
 * a rendered sentence passes against an empty `en.json` and proves nothing.
 *
 * Three of these rules exist because the naive version shipped and was WRONG on
 * screen while the fold's own tests stayed green: one shared `limit` label
 * invented a per-file cap that does not exist, and expansion's counts rendered
 * as two numbers describing neither thing.
 */

import { describe, expect, it } from 'vitest';

import type { TraceDetail, TraceOutcome, TraceStage } from '$lib/types/chat';
import {
  OUTCOME_MARKERS,
  detailChips,
  formatMs,
  markerClass,
  outcomeLabelKey,
  reasonLabelKey,
  sourceLabelKey,
  stageLabelKey,
  subjectLabelKey,
} from './traceLabels';
import type { TraceNode } from './traceTree';

function node(
  stage: TraceStage,
  detail: TraceDetail,
  outcome: TraceOutcome = 'ok',
  nodeId: string | null = null
): TraceNode {
  return {
    key: `root::${nodeId ?? stage}`,
    nodeId,
    stage,
    labelStage: stage,
    outcome,
    detail,
    children: [],
    seq: 1,
    updatedSeq: 1,
  };
}

const keys = (chips: { key: string }[]) => chips.map((c) => c.key);

describe('detailChips', () => {
  it('renders an explicit zero for an empty outcome', () => {
    // "We looked and found nothing" must be visibly different from "we never
    // looked" — a blank count reads as the latter.
    const chips = detailChips(node('found', { count: 0 }, 'empty'));
    expect(keys(chips)).toEqual(['chat.trace.detail.found']);
    expect(chips[0].params).toEqual({ count: 0 });
  });

  it('renders no count at all for a skipped outcome', () => {
    // The control for the case above. A skipped stage has nothing to count, so
    // a `0` there would claim a measurement that was never taken.
    expect(detailChips(node('reranked', { count: 12, kept: 3 }, 'skipped'))).toEqual([]);
  });

  it('labels a sampled limit as a per-file cap', () => {
    const chips = detailChips(node('sampled', { kept: 12, dropped: 36, limit: 4 }));
    expect(keys(chips)).toEqual(['chat.trace.detail.keptOfDropped', 'chat.trace.detail.perFile']);
  });

  it('labels a budget limit as characters, never as a per-file cap', () => {
    // The bug this pins: one shared label rendered the excerpt budget as
    // "max 92096/file", inventing a per-file ceiling that does not exist.
    const chips = detailChips(node('budgeted', { kept: 9, dropped: 3, limit: 92096 }));
    expect(keys(chips)).toContain('chat.trace.detail.budgetChars');
    expect(keys(chips)).not.toContain('chat.trace.detail.perFile');
  });

  it('drops a limit whose meaning is not known for that stage', () => {
    // Mislabelling a bound is worse than omitting it, so an unrecognised stage
    // renders no limit chip rather than guessing which bound it is.
    const chips = detailChips(node('found', { count: 4, limit: 7 }));
    expect(keys(chips)).toEqual(['chat.trace.detail.found']);
  });

  it('reports expansion as one widened-of-kept chip', () => {
    const chips = detailChips(node('expanded', { count: 4, kept: 12 }));
    expect(keys(chips)).toEqual(['chat.trace.detail.widened']);
    expect(chips[0].params).toEqual({ count: 4, kept: 12 });
  });

  it('does not render expansion through the generic kept/count pair', () => {
    // The control for the case above: the generic path would produce
    // "12 kept" then "4 found" — two numbers describing neither thing.
    const chips = detailChips(node('expanded', { count: 4, kept: 12 }));
    expect(keys(chips)).not.toContain('chat.trace.detail.kept');
    expect(keys(chips)).not.toContain('chat.trace.detail.found');
  });

  it('still uses the generic pair for a stage that is not expansion', () => {
    // ...and the control for THAT: the special case must be scoped to the
    // stage, not applied to every node carrying both keys.
    const chips = detailChips(node('filtered', { count: 4, kept: 12 }));
    expect(keys(chips)).toEqual(['chat.trace.detail.kept', 'chat.trace.detail.found']);
  });
});

describe('markers', () => {
  it('gives empty and skipped different marker families', () => {
    // The pair the whole panel exists to separate. Asserting they merely
    // differ is not enough — a fill difference is what low-vision and
    // colourblind readers miss, so the shapes must be ring versus dash.
    expect(OUTCOME_MARKERS.empty).toBe('ring');
    expect(OUTCOME_MARKERS.skipped).toBe('dash');
    expect(markerClass('empty')).not.toBe(markerClass('skipped'));
  });

  it('gives all six outcomes distinct markers', () => {
    const markers = Object.values(OUTCOME_MARKERS);
    expect(markers).toHaveLength(6);
    expect(new Set(markers).size).toBe(6);
  });
});

describe('label keys', () => {
  it('names only the two ambiguous filter subjects', () => {
    // FILTERED fires twice per turn; two rows both reading "Filtered" looks
    // like a bug rather than two different filters.
    expect(subjectLabelKey('quarantine')).toBe('chat.trace.node.quarantine');
    expect(subjectLabelKey('masking')).toBe('chat.trace.node.masking');
    expect(subjectLabelKey('plan')).toBeNull();
    expect(subjectLabelKey(null)).toBeNull();
  });

  it('prefers the plane over the system when a node reports both', () => {
    expect(sourceLabelKey({ plane: 'chunk', source: 'cache' })).toBe('chat.trace.plane.chunk');
    expect(sourceLabelKey({ source: 'cache' })).toBe('chat.trace.source.cache');
    expect(sourceLabelKey({})).toBeNull();
  });

  it('builds stage, outcome and reason keys from the raw code', () => {
    // A `reason` the backend adds after this client ships must render as
    // itself; the component's `defaultValue` relies on the key being derived
    // from the code rather than looked up in a client-side table.
    expect(stageLabelKey('cache_lookup')).toBe('chat.trace.stage.cache_lookup');
    expect(outcomeLabelKey('declined')).toBe('chat.trace.outcome.declined');
    expect(reasonLabelKey('quota_guard')).toBe('chat.trace.reason.quota_guard');
  });
});

describe('formatMs', () => {
  it('reports sub-millisecond work as 0ms rather than hiding it', () => {
    // A stage that took no measurable time still RAN. Blanking the timing
    // would make it look skipped.
    const formatted = formatMs(0.2);
    expect(formatted).toEqual({ key: 'chat.trace.timing.ms', params: { ms: 0 } });
  });

  it('switches to seconds at a second, to one decimal', () => {
    expect(formatMs(7120)).toEqual({ key: 'chat.trace.timing.s', params: { s: 7.1 } });
    expect(formatMs(999)).toEqual({ key: 'chat.trace.timing.ms', params: { ms: 999 } });
  });

  it('renders nothing when a node carries no timing', () => {
    expect(formatMs(undefined)).toBeNull();
    expect(formatMs(Number.NaN)).toBeNull();
  });
});
