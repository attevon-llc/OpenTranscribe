/**
 * How one trace node renders (GH #514).
 *
 * The load-bearing test here is `empty` versus `skipped`. Everything else in
 * this feature is plumbing; that distinction — "we looked and found nothing"
 * versus "we never looked" — is the reason the panel exists, and if the two
 * render alike the whole thing is decorative.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import type { TraceDetail, TraceOutcome, TraceStage } from '$lib/types/chat';
import type { TraceNode } from '$lib/chat/traceTree';

// Identity translator: vitest boots no locale bundle, so `$t` returns the raw
// key. Assertions therefore check WHICH KEY a state selects — asserting on
// rendered English here would be vacuous and would pass with an empty en.json.
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, params?: unknown) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import ChatTraceNode from './ChatTraceNode.svelte';

function node(over: Partial<TraceNode> = {}): TraceNode {
  return {
    key: 'turn::main',
    nodeId: 'main',
    stage: 'found' as TraceStage,
    outcome: 'ok' as TraceOutcome,
    detail: {} as TraceDetail,
    children: [],
    seq: 1,
    updatedSeq: 1,
    ...over,
    // A leg is labelled by the stage that created it, so a test overriding
    // `stage` gets a matching label unless it deliberately sets one.
    labelStage: over.labelStage ?? over.stage ?? ('found' as TraceStage),
  };
}

/** The rendered row's class list — asserted directly, so the check names the
 *  state it means rather than "an element matched". */
const rowClass = (container: HTMLElement): string =>
  container.querySelector('[data-testid="trace-node"]')?.className ?? '';

const renderNode = (over: Partial<TraceNode> = {}, props: Record<string, unknown> = {}) =>
  render(ChatTraceNode, {
    props: { node: node(over), visible: new Set(['turn::main']), ...props },
  });

describe('ChatTraceNode', () => {
  it('renders "empty" and "skipped" with different markers, opacity and words', () => {
    // THE test. These two produce the same answer and the same absence of
    // results; only the panel can tell them apart, and only if it renders them
    // differently by SHAPE — a colour or fill difference is what low-vision and
    // colourblind readers miss.
    const { container: emptyEl, unmount } = renderNode({
      outcome: 'empty',
      detail: { count: 0 },
    });
    const emptyRow = emptyEl.querySelector('[data-testid="trace-node"]');
    const emptyMarker = emptyEl.querySelector('.trace-marker');
    expect(emptyRow?.getAttribute('data-outcome')).toBe('empty');
    expect(emptyMarker?.className).toContain('trace-marker--ring');
    expect(screen.getByTestId('trace-outcome')).toHaveTextContent('chat.trace.outcome.empty');
    unmount();

    const { container: skippedEl } = renderNode({
      outcome: 'skipped',
      detail: { reason: 'disabled' },
    });
    const skippedRow = skippedEl.querySelector('[data-testid="trace-node"]');
    const skippedMarker = skippedEl.querySelector('.trace-marker');
    expect(skippedRow?.getAttribute('data-outcome')).toBe('skipped');
    expect(skippedMarker?.className).toContain('trace-marker--dash');
    expect(screen.getByTestId('trace-outcome')).toHaveTextContent('chat.trace.outcome.skipped');

    // Different marker FAMILY, not merely a different fill of the same shape.
    expect(skippedMarker?.className).not.toContain('trace-marker--ring');
  });

  it('gives every one of the six outcomes its own marker shape', () => {
    // Guards against two outcomes collapsing onto one shape as the set grows.
    const shapes = new Set<string>();
    for (const outcome of ['ok', 'empty', 'skipped', 'cached', 'declined', 'failed'] as const) {
      const { container, unmount } = renderNode({ outcome });
      const marker = container.querySelector('.trace-marker');
      const shape = (marker?.className ?? '')
        .split(' ')
        .find((c) => c.startsWith('trace-marker--'));
      expect(shape, `outcome ${outcome} has no marker shape`).toBeTruthy();
      shapes.add(shape as string);
      unmount();
    }
    expect(shapes.size).toBe(6);
  });

  it('renders an explicit zero for an empty leg rather than omitting the count', () => {
    // A blank count reads as "we never looked". The zero is what makes "we
    // looked and found nothing" legible.
    renderNode({ outcome: 'empty', detail: { count: 0 } });
    expect(screen.getByText('chat.trace.detail.found')).toBeInTheDocument();
  });

  it('shows no count at all on a skipped node, because there is nothing to count', () => {
    renderNode({ outcome: 'skipped', detail: { count: 0, reason: 'cached' } });
    expect(screen.queryByText('chat.trace.detail.found')).not.toBeInTheDocument();
  });

  it('falls back to the raw reason code when the client has no label for it', () => {
    // The backend's `reason` vocabulary can grow without a client release.
    // Rendering `chat.trace.reason.quota_guard` to a user is worse than
    // rendering `quota_guard`.
    renderNode({ outcome: 'declined', detail: { reason: 'quota_guard' } });
    expect(screen.getByText('chat.trace.reason.quota_guard')).toBeInTheDocument();
  });

  it('marks a node pending only while the turn is still streaming', () => {
    // Guards a stuck "still running" pulse on a completed or reloaded turn.
    const { container, unmount } = renderNode({ stage: 'fanned_vector' }, { streaming: true });
    expect(rowClass(container)).toContain('trace-node--pending');
    unmount();

    const { container: settled } = renderNode({ stage: 'fanned_vector' }, { streaming: false });
    expect(rowClass(settled)).not.toContain('trace-node--pending');
  });

  it('does not mark a resolved node pending even mid-stream', () => {
    // The control for the test above: a leg that has reported `found` is done,
    // even though the turn as a whole is still going.
    const { container } = renderNode({ stage: 'found' }, { streaming: true });
    expect(rowClass(container)).not.toContain('trace-node--pending');
  });

  it('names the data source it touched, which is the whole point of the panel', () => {
    const { unmount } = renderNode({ detail: { plane: 'chunk' } });
    expect(screen.getByText('chat.trace.plane.chunk')).toBeInTheDocument();
    unmount();

    renderNode({ detail: { source: 'postgres' } });
    expect(screen.getByText('chat.trace.source.postgres')).toBeInTheDocument();
  });

  it('disables its entrance animation under reduced motion', () => {
    // The pacer and the CSS rule cover two of the three mechanisms; this is the
    // third, and without it the tree is not literally readable as a static list.
    const { container: still } = renderNode({}, { reducedMotion: true });
    expect(rowClass(still)).toContain('trace-node--static');

    // Control: the class is not always on, or "disables animation" would be
    // satisfied by a component that never animates in the first place.
    const { container: moving } = renderNode({}, { reducedMotion: false });
    expect(rowClass(moving)).not.toContain('trace-node--static');
  });
});
