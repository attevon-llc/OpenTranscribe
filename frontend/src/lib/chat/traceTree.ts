/**
 * Fold `trace` SSE frames into a renderable tree (GH #514).
 *
 * Pure and DOM-free on purpose: this is where all the interesting correctness
 * lives, so it is the cheapest place to test properly. The component layer above
 * it only maps nodes to markup.
 *
 * Four rules, each of which exists because the naive version is wrong:
 *
 * 1. **A leg reports itself TWICE under one node id.** `legs.py` emits a
 *    `fanned_*` when a leg starts and a `found` when it finishes. Those collapse
 *    into ONE node whose stage/outcome/detail advance — not two rows. This is
 *    also what gives every node a pending -> resolved transition to animate.
 * 2. **Never regress a node's stage** on a late or duplicated frame, but always
 *    merge `detail`, so the `plane` from the start frame survives the finish
 *    frame that carries the counts.
 * 3. **Orphans must resolve.** A frame whose `parent` has not arrived yet is
 *    parked and re-parented later. The backend's own docstring says not to
 *    assume stage ordering, and a fan-out completes out of order by design.
 * 4. **Never mutate a node in place.** Svelte reactivity keys off object
 *    identity; mutating a nested node under a spread-only store update is the
 *    shape whose re-render behaviour is compiler-version-dependent. The pure
 *    fold's own tests would pass while the DOM never repainted.
 */

import type { TraceDetail, TraceOutcome, TraceStage } from '$lib/types/chat';

/** One frame off the wire. */
export interface TraceFrame {
  seq: number;
  stage: TraceStage;
  outcome: TraceOutcome;
  parent: string | null;
  node_id: string | null;
  detail: TraceDetail;
}

export interface TraceNode {
  /** Stable across updates. Synthesized — NOT the wire `node_id`. */
  key: string;
  /** The raw wire id, used to resolve other frames' `parent` references. */
  nodeId: string | null;
  stage: TraceStage;
  /**
   * The stage that CREATED this node, which is what it is LABELLED by.
   *
   * A leg reports `fanned_vector` then `found` under one id. Labelling by the
   * latest stage renamed a resolved leg from "Search" to "Found", so the row
   * read "Found · chunk plane · 48 found" — redundant, and it lost what the
   * node actually is. The outcome badge and count already say how it went.
   */
  labelStage: TraceStage;
  outcome: TraceOutcome;
  detail: TraceDetail;
  children: TraceNode[];
  /** Delivery order of the first frame for this node — the render order. */
  seq: number;
  /** Delivery order of the most recent frame, so a renderer can spot changes. */
  updatedSeq: number;
  /**
   * Set while this node is parked at the root waiting for a parent that has not
   * arrived. Cleared when `reparentOrphans` claims it. An explicit field rather
   * than an inferred one: "is this a real root or a waiting child" is not
   * recoverable afterwards, and guessing it would silently flatten a fan-out.
   */
  orphanOf?: string | null;
}

export interface TraceState {
  roots: TraceNode[];
  /** Frames the recorder dropped. Never silent — the panel says so. */
  truncated: boolean;
}

/**
 * Canonical stage order, used ONLY to decide whether a frame advances a node.
 *
 * ⚠️ Do not sort the rendered tree by this. Nodes render in EMISSION order, so a
 * stage running out of sequence is visible rather than tidied away — which is
 * precisely the kind of thing this panel is for.
 */
const STAGE_ORDER: TraceStage[] = [
  'submitted',
  'validated',
  'parsed_names',
  'rewritten',
  'cache_lookup',
  'planned',
  'fanned_relational',
  'fanned_vector',
  'found',
  'reranked',
  'sampled',
  'expanded',
  'filtered',
  'budgeted',
  'reviewed',
  'presented',
];

const stageRank = (stage: TraceStage): number => {
  const index = STAGE_ORDER.indexOf(stage);
  // An unknown stage from a newer backend sorts last rather than first, so it
  // is treated as an advance and never silently discarded.
  return index === -1 ? STAGE_ORDER.length : index;
};

/**
 * Stages that mean "this node has started but not reported a result".
 * A leg always follows one of these with a `found` under the same node id.
 */
const PENDING_STAGES = new Set<TraceStage>(['fanned_vector', 'fanned_relational']);

export const isPending = (node: TraceNode): boolean => PENDING_STAGES.has(node.stage);

/**
 * Node identity. A frame carrying a `node_id` always keys on it, so a leg's
 * start and finish frames — same id, different stage — become one node. A frame
 * without one keys on its own stage, which is safe only because every such
 * emitter fires at most once per parent.
 */
export const traceNodeKey = (frame: TraceFrame): string => {
  // A node reporting its OWN progress may name itself as parent. Normalising
  // that to a root keeps `root::turn` and `turn::turn` one node instead of two —
  // without this the turn node forks into a duplicate the moment `presented`
  // arrives, and the panel shows the same row twice.
  const parent = frame.parent === frame.node_id ? null : frame.parent;
  return `${parent ?? 'root'}::${frame.node_id ?? frame.stage}`;
};

export const emptyTraceState = (): TraceState => ({ roots: [], truncated: false });

/** Depth-first copy, replacing exactly one node and every ancestor of it. */
function replaceNode(nodes: TraceNode[], key: string, next: TraceNode): TraceNode[] {
  return nodes.map((node) => {
    if (node.key === key) return next;
    const children = replaceNode(node.children, key, next);
    return children === node.children ? node : { ...node, children };
  });
}

function findNode(nodes: TraceNode[], predicate: (n: TraceNode) => boolean): TraceNode | null {
  for (const node of nodes) {
    if (predicate(node)) return node;
    const hit = findNode(node.children, predicate);
    if (hit) return hit;
  }
  return null;
}

function appendChild(nodes: TraceNode[], parentKey: string, child: TraceNode): TraceNode[] {
  return nodes.map((node) => {
    if (node.key === parentKey) return { ...node, children: [...node.children, child] };
    const children = appendChild(node.children, parentKey, child);
    return children === node.children ? node : { ...node, children };
  });
}

/**
 * Fold one frame into the tree, returning a NEW state.
 *
 * @param prev - The state so far, or undefined for the first frame of a turn.
 * @param frame - One `trace` SSE frame.
 */
export function applyTraceFrame(prev: TraceState | undefined, frame: TraceFrame): TraceState {
  const state = prev ?? emptyTraceState();
  const key = traceNodeKey(frame);
  const existing = findNode(state.roots, (n) => n.key === key);

  if (existing) {
    // Rule 2: advance, never regress — but always fold in new detail, so the
    // `plane` a start frame carried survives the finish frame's counts.
    const advances = stageRank(frame.stage) >= stageRank(existing.stage);
    const next: TraceNode = {
      ...existing,
      stage: advances ? frame.stage : existing.stage,
      outcome: advances ? frame.outcome : existing.outcome,
      detail: { ...existing.detail, ...frame.detail },
      updatedSeq: Math.max(existing.updatedSeq, frame.seq),
    };
    return { ...state, roots: replaceNode(state.roots, key, next) };
  }

  const node: TraceNode = {
    key,
    nodeId: frame.node_id,
    stage: frame.stage,
    labelStage: frame.stage,
    outcome: frame.outcome,
    detail: { ...frame.detail },
    children: [],
    seq: frame.seq,
    updatedSeq: frame.seq,
  };

  // A node whose parent is itself (`node_id === parent`) is a root reporting its
  // own progress — the turn node does exactly this — and must not become its own
  // child.
  if (frame.parent === null || frame.parent === frame.node_id) {
    return { ...state, roots: [...state.roots, node] };
  }

  const parent = findNode(state.roots, (n) => n.nodeId === frame.parent);
  if (parent) {
    return { ...state, roots: appendChild(state.roots, parent.key, node) };
  }

  // Rule 3: the parent has not arrived. Park at the root so the node is never
  // lost, and let `reparentOrphans` claim it once the parent shows up. Dropping
  // it instead would hide a whole leg.
  return { ...state, roots: [...state.roots, { ...node, orphanOf: frame.parent }] };
}

/**
 * Re-home any root that now has a resolvable parent.
 *
 * Called after each fold. Separate from `applyTraceFrame` so the parking rule
 * and the claiming rule can be read — and tested — independently.
 */
export function reparentOrphans(state: TraceState): TraceState {
  const byId = new Map<string, TraceNode>();
  for (const node of state.roots) {
    if (node.nodeId) byId.set(node.nodeId, node);
  }

  const claimed = new Set<string>();
  let roots = state.roots;
  for (const node of state.roots) {
    const parentId = orphanParent(node);
    if (!parentId) continue;
    const parent = byId.get(parentId);
    if (!parent || parent.key === node.key) continue;
    roots = appendChild(
      roots.filter((n) => n.key !== node.key),
      parent.key,
      { ...node, orphanOf: null }
    );
    claimed.add(node.key);
  }
  return claimed.size ? { ...state, roots } : state;
}

/** The parent a parked node is still waiting for; `null` for a genuine root. */
function orphanParent(node: TraceNode): string | null {
  return node.orphanOf ?? null;
}

/** Fold a frame and settle any orphans it may have resolved. */
export function foldTraceFrame(prev: TraceState | undefined, frame: TraceFrame): TraceState {
  const parked = applyTraceFrame(prev, frame);
  return reparentOrphans(parked);
}

/** Mark the trace as truncated — carried on the `done` frame, not a stage. */
export function markTruncated(state: TraceState | undefined): TraceState {
  return { ...(state ?? emptyTraceState()), truncated: true };
}

/** Every node, depth-first, in render order. Handy for tests and counts. */
export function flattenTrace(state: TraceState | undefined): TraceNode[] {
  const out: TraceNode[] = [];
  const walk = (nodes: TraceNode[]) => {
    for (const node of nodes) {
      out.push(node);
      walk(node.children);
    }
  };
  walk(state?.roots ?? []);
  return out;
}
