/**
 * The trace fold (GH #514).
 *
 * These are the tests that matter most on the client: the component layer only
 * maps nodes to markup, so every way the panel can lie about what the pipeline
 * did is decided here.
 */

import { describe, expect, it } from 'vitest';
import type { TraceFrame } from './traceTree';
import {
  emptyTraceState,
  flattenTrace,
  foldTraceFrame,
  isPending,
  markTruncated,
  traceNodeKey,
} from './traceTree';

let seq = 0;
const frame = (over: Partial<TraceFrame> = {}): TraceFrame => ({
  seq: ++seq,
  stage: 'found',
  outcome: 'ok',
  parent: 'turn',
  node_id: 'main',
  detail: {},
  ...over,
});

const foldAll = (frames: TraceFrame[]) => {
  let state = emptyTraceState();
  for (const f of frames) state = foldTraceFrame(state, f);
  return state;
};

const byKey = (state: ReturnType<typeof foldAll>, key: string) =>
  flattenTrace(state).find((n) => n.key === key);

describe('traceTree fold', () => {
  it('collapses a leg start frame and its finish frame into ONE node', () => {
    // `legs.py` emits `fanned_vector` when a leg starts and `found` when it
    // finishes, both under the same node id. Two rows would double-count every
    // leg and destroy the pending -> resolved transition the panel animates.
    const state = foldAll([
      frame({ stage: 'fanned_vector', outcome: 'ok', detail: { plane: 'chunk' } }),
      frame({ stage: 'found', outcome: 'ok', detail: { count: 42, ms: 118 } }),
    ]);

    const nodes = flattenTrace(state);
    expect(nodes).toHaveLength(1);
    expect(nodes[0].stage).toBe('found');
    // Detail from BOTH frames survives — the plane came from the start frame.
    expect(nodes[0].detail).toEqual({ plane: 'chunk', count: 42, ms: 118 });
  });

  it('does not regress a node when a stale earlier-stage frame arrives late', () => {
    const state = foldAll([
      frame({ stage: 'found', outcome: 'ok', detail: { count: 42 } }),
      frame({ stage: 'fanned_vector', outcome: 'ok', detail: { plane: 'chunk' } }),
    ]);

    const nodes = flattenTrace(state);
    expect(nodes[0].stage).toBe('found');
    expect(nodes[0].outcome).toBe('ok');
    expect(nodes[0].detail.plane).toBe('chunk');
  });

  it('re-parents an orphan once its parent frame finally arrives', () => {
    // A child can legitimately arrive before its parent: the backend's own
    // docstring says not to assume stage ordering, and a fan-out resolves out
    // of order by design. Dropping the child would hide a whole leg.
    const state = foldAll([
      frame({ stage: 'found', parent: 'plan', node_id: 'subquestion-0' }),
      frame({ stage: 'planned', parent: 'turn', node_id: 'plan', detail: { legs: 2 } }),
    ]);

    const plan = byKey(state, traceNodeKey({ ...frame({}), parent: 'turn', node_id: 'plan' }));
    expect(plan).toBeDefined();
    expect(plan?.children.map((c) => c.nodeId)).toEqual(['subquestion-0']);
    expect(state.roots.some((n) => n.nodeId === 'subquestion-0')).toBe(false);
  });

  it('nests fan-out siblings under one parent instead of flattening them', () => {
    const state = foldAll([
      frame({ stage: 'planned', parent: 'turn', node_id: 'plan', detail: { legs: 3 } }),
      frame({ stage: 'fanned_vector', parent: 'plan', node_id: 'main' }),
      frame({ stage: 'fanned_vector', parent: 'plan', node_id: 'subquestion-0' }),
      frame({ stage: 'fanned_relational', parent: 'plan', node_id: 'counted' }),
    ]);

    const plan = flattenTrace(state).find((n) => n.nodeId === 'plan');
    expect(plan?.children.map((c) => c.nodeId)).toEqual(['main', 'subquestion-0', 'counted']);
  });

  it('keeps empty and skipped as distinct outcomes on distinct nodes', () => {
    // The pair this whole feature exists to separate: "we looked and found
    // nothing" versus "we never looked". Collapsing them would make the panel
    // decorative.
    const state = foldAll([
      frame({ stage: 'found', outcome: 'empty', node_id: 'main', detail: { count: 0 } }),
      frame({
        stage: 'reranked',
        outcome: 'skipped',
        node_id: 'rerank',
        detail: { reason: 'disabled' },
      }),
    ]);

    const nodes = flattenTrace(state);
    const found = nodes.find((n) => n.nodeId === 'main');
    const rerank = nodes.find((n) => n.nodeId === 'rerank');
    expect(found?.outcome).toBe('empty');
    expect(found?.detail.count).toBe(0);
    expect(rerank?.outcome).toBe('skipped');
    expect(rerank?.detail.count).toBeUndefined();
  });

  it('never mutates a previous state, so Svelte sees a new object', () => {
    // Rule 4. In-place mutation under a spread-only store update is the shape
    // whose re-render behaviour is compiler-version-dependent: the fold's own
    // assertions would pass while the DOM never repainted.
    const first = foldTraceFrame(emptyTraceState(), frame({ stage: 'fanned_vector' }));
    const snapshot = JSON.parse(JSON.stringify(first));

    const second = foldTraceFrame(first, frame({ stage: 'found', detail: { count: 9 } }));

    expect(first).toEqual(snapshot);
    expect(second).not.toBe(first);
    expect(second.roots).not.toBe(first.roots);
    expect(second.roots[0]).not.toBe(first.roots[0]);
  });

  it('treats a node reporting its own progress as a root, not its own child', () => {
    const state = foldAll([
      frame({ stage: 'submitted', parent: null, node_id: 'turn' }),
      frame({ stage: 'validated', parent: null, node_id: 'turn' }),
      frame({ stage: 'presented', parent: 'turn', node_id: 'turn', detail: { count: 9 } }),
    ]);

    expect(state.roots).toHaveLength(1);
    expect(state.roots[0].children).toHaveLength(0);
    expect(state.roots[0].stage).toBe('presented');
  });

  it('marks a pending node only while it has started and not resolved', () => {
    const started = foldTraceFrame(emptyTraceState(), frame({ stage: 'fanned_vector' }));
    expect(isPending(started.roots[0])).toBe(true);

    const resolved = foldTraceFrame(started, frame({ stage: 'found', detail: { count: 1 } }));
    expect(isPending(resolved.roots[0])).toBe(false);
  });

  it('records truncation without inventing a stage for it', () => {
    const state = foldAll([frame({ stage: 'found' })]);
    expect(state.truncated).toBe(false);

    const truncated = markTruncated(state);
    expect(truncated.truncated).toBe(true);
    expect(flattenTrace(truncated)).toHaveLength(flattenTrace(state).length);
  });

  it('keeps an unknown stage from a newer backend rather than discarding it', () => {
    // Forward compatibility: an unrecognised stage must still render as a node.
    const state = foldAll([
      frame({ stage: 'found', node_id: 'main' }),
      frame({ stage: 'some_future_stage' as never, node_id: 'novel', detail: { count: 1 } }),
    ]);

    expect(flattenTrace(state).map((n) => n.nodeId)).toContain('novel');
  });

  it('renders in emission order, not canonical stage order', () => {
    // Sorting by the canonical sequence would tidy away a stage that ran out of
    // order — exactly the thing worth seeing.
    const state = foldAll([
      frame({ stage: 'reranked', parent: null, node_id: 'rerank' }),
      frame({ stage: 'submitted', parent: null, node_id: 'turn' }),
    ]);

    expect(state.roots.map((n) => n.nodeId)).toEqual(['rerank', 'turn']);
  });
});
