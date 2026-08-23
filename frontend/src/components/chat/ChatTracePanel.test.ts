/**
 * The trace panel shell (GH #514).
 *
 * The empty states carry more weight here than they look: a panel that renders
 * nothing is the normal case for a reloaded conversation (traces are live-only),
 * and if that reads as "broken" rather than "not stored", the honest design
 * decision behind it becomes a bug report.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import type { TraceState } from '$lib/chat/traceTree';
import { emptyTraceState, foldTraceFrame, markTruncated } from '$lib/chat/traceTree';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, params?: unknown) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import ChatTracePanel from './ChatTracePanel.svelte';
// Svelte 5 removed `component.$on(...)`, so a legacy dispatched event is only
// observable through an `on:event` listener in a consumer's markup — the same
// reason ChatContextBarTestHost exists.
import ChatTracePanelTestHost from './ChatTracePanelTestHost.svelte';

let seq = 0;
const traceWith = (count = 3): TraceState => {
  let state = emptyTraceState();
  for (let i = 0; i < count; i += 1) {
    state = foldTraceFrame(state, {
      seq: ++seq,
      stage: 'found',
      outcome: 'ok',
      parent: null,
      node_id: `n${i}`,
      detail: { count: i },
    });
  }
  return state;
};

const open = (props: Record<string, unknown> = {}) =>
  render(ChatTracePanel, { props: { open: true, ...props } });

describe('ChatTracePanel', () => {
  it('renders nothing at all while closed', () => {
    render(ChatTracePanel, { props: { open: false, trace: traceWith() } });
    expect(screen.queryByTestId('chat-trace-panel')).not.toBeInTheDocument();
  });

  it('says traces are NOT STORED on a reloaded turn, rather than looking broken', () => {
    // The single most visible consequence of the live-only decision. A bare
    // blank panel would be read as a defect and reported as one.
    open({ trace: undefined, streaming: false });
    expect(screen.getByTestId('chat-trace-empty')).toHaveTextContent('chat.trace.empty.notStored');
  });

  it('invites a question on a thread that has no turn yet', () => {
    // The toggle is deliberately not gated on an existing turn — the panel's
    // whole claim is that you can WATCH retrieval happen, and gating it meant
    // the first question of a conversation could only be inspected after its
    // answer had finished. Opening it on an empty thread must not claim a
    // trace "was not stored" for a question nobody asked.
    open({ trace: undefined, streaming: false, hasTurn: false });
    expect(screen.getByTestId('chat-trace-empty')).toHaveTextContent('chat.trace.empty.noTurnYet');
  });

  it('still says not-stored for a real turn whose trace is gone', () => {
    // The control for the case above: `noTurnYet` is checked first, so it must
    // not swallow the reloaded-turn state that every other empty case is
    // measured against.
    open({ trace: undefined, streaming: false, hasTurn: true });
    expect(screen.getByTestId('chat-trace-empty')).toHaveTextContent('chat.trace.empty.notStored');
  });

  it('distinguishes waiting, failed-early and context-off from not-stored', () => {
    // Four different facts. Collapsing them into one message would make a
    // legitimately empty panel indistinguishable from a broken one.
    const { unmount: a } = open({ streaming: true });
    expect(screen.getByTestId('chat-trace-empty')).toHaveTextContent('chat.trace.empty.waiting');
    a();

    const { unmount: b } = open({ failedEarly: true });
    expect(screen.getByTestId('chat-trace-empty')).toHaveTextContent(
      'chat.trace.empty.failedEarly'
    );
    b();

    open({ contextOff: true });
    expect(screen.getByTestId('chat-trace-empty')).toHaveTextContent('chat.trace.empty.contextOff');
  });

  it('shows a truncation notice that cannot be dismissed', () => {
    // A shortened tree that does not say so is a trace that lies about what
    // ran, which is precisely the failure this panel exists to surface.
    open({ trace: markTruncated(traceWith()), streaming: false });

    const notice = screen.getByTestId('chat-trace-truncated');
    expect(notice).toHaveTextContent('chat.trace.truncated');
    expect(notice.querySelector('button')).toBeNull();
  });

  it('does not claim truncation on an ordinary complete trace', () => {
    // Control: a permanently-visible banner is one nobody reads.
    open({ trace: traceWith(), streaming: false });
    expect(screen.queryByTestId('chat-trace-truncated')).not.toBeInTheDocument();
  });

  it('always carries a beta chip with an explanatory title', () => {
    // A standing caveat, not a state: it must be there on a quiet panel too,
    // because the whole point is to set expectations BEFORE a defect does.
    open({ trace: traceWith(), streaming: false });

    const chip = screen.getByTestId('chat-trace-beta');
    expect(chip).toHaveTextContent('chat.trace.beta');
    // The hover text is the substance; a bare "Beta" chip explains nothing.
    expect(chip).toHaveAttribute('title', 'chat.trace.betaTitle');
  });

  it('shows the beta chip even with no trace at all', () => {
    // The control for the case above — an empty panel is exactly where someone
    // is most likely to think the feature is broken rather than young.
    open({ trace: undefined, streaming: false });
    expect(screen.getByTestId('chat-trace-beta')).toBeInTheDocument();
  });

  it('marks itself live only while the turn is streaming', () => {
    const { unmount } = open({ trace: traceWith(), streaming: true });
    expect(screen.getByTestId('chat-trace-live')).toBeInTheDocument();
    unmount();

    open({ trace: traceWith(), streaming: false });
    expect(screen.queryByTestId('chat-trace-live')).not.toBeInTheDocument();
  });

  it('emits close from its own button', async () => {
    const onClose = vi.fn();
    render(ChatTracePanelTestHost, { props: { trace: traceWith(), onClose } });

    await fireEvent.click(screen.getByLabelText('chat.trace.close'));

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on Escape AND stops the key reaching the page', async () => {
    // The page listens for Escape on window and uses it to CANCEL an in-flight
    // generation. Without stopPropagation, closing this panel would abort the
    // user's answer — so the containment matters as much as the close.
    const pageHandler = vi.fn();
    window.addEventListener('keydown', pageHandler);
    const onClose = vi.fn();
    render(ChatTracePanelTestHost, { props: { trace: traceWith(), onClose } });

    await fireEvent.keyDown(screen.getByTestId('chat-trace-panel'), { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(pageHandler).not.toHaveBeenCalled();
    window.removeEventListener('keydown', pageHandler);
  });

  it('is a complementary landmark, not a tree, and adds no live region', () => {
    // `role="tree"` would promise arrow-key navigation this read-only panel does
    // not have, and a second aria-live region would double-narrate every stage
    // against ChatStatusIndicator.
    const { container } = open({ trace: traceWith(), streaming: true });

    const panel = screen.getByTestId('chat-trace-panel');
    expect(panel.tagName).toBe('ASIDE');
    expect(container.querySelector('[role="tree"]')).toBeNull();
    expect(container.querySelector('[aria-live]')).toBeNull();
  });
});
