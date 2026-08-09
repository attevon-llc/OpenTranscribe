import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

// Identity translator (same pattern as OIDCSettings.discoveredClaims.test.ts): returns the
// raw dot-notation key so assertions can check which key the component picked for a given
// state, without needing to boot real i18next + locale JSON in the test environment.
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import ChatReasoning from './ChatReasoning.svelte';

describe('ChatReasoning', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders nothing when there is no reasoning content', () => {
    const { container } = render(ChatReasoning, { props: { content: '' } });
    expect(container.querySelector('[data-testid="chat-reasoning"]')).toBeNull();
  });

  it('is collapsed by default and expands the reasoning text on click', async () => {
    render(ChatReasoning, { props: { content: 'because the transcript says so' } });

    expect(screen.queryByTestId('chat-reasoning-body')).toBeNull();

    await fireEvent.click(screen.getByRole('button'));

    expect(screen.getByTestId('chat-reasoning-body')).toBeInTheDocument();
    expect(screen.getByText(/because the transcript says so/)).toBeInTheDocument();
  });

  it('picks the generic label for a message reloaded from history (no timing data)', () => {
    render(ChatReasoning, { props: { content: 'reasoning text', streaming: false } });
    expect(screen.getByRole('button')).toHaveTextContent('chat.reasoning.label');
  });

  it('picks "thought for" once reasoning has a frozen duration', () => {
    render(ChatReasoning, {
      props: { content: 'reasoning text', streaming: false, durationMs: 4200 },
    });
    expect(screen.getByRole('button')).toHaveTextContent('chat.reasoning.thoughtFor');
  });

  it('picks a bare "thinking" label the instant streaming starts (0 elapsed seconds)', () => {
    const startedAt = Date.now();
    render(ChatReasoning, {
      props: { content: 'still going', streaming: true, startedAt },
    });
    expect(screen.getByRole('button')).toHaveTextContent('chat.reasoning.thinking');
    expect(screen.getByRole('button')).not.toHaveTextContent('chat.reasoning.thinkingFor');
  });

  it('switches to the live "thinking for" label once elapsed time ticks past zero', async () => {
    const startedAt = Date.now();
    render(ChatReasoning, {
      props: { content: 'still going', streaming: true, startedAt },
    });

    await vi.advanceTimersByTimeAsync(3000);

    expect(screen.getByRole('button')).toHaveTextContent('chat.reasoning.thinkingFor');
  });

  it('never shows a collapsed-content region until expanded, even while streaming', () => {
    render(ChatReasoning, {
      props: { content: 'partial reasoning so far…', streaming: true, startedAt: Date.now() },
    });
    expect(screen.queryByTestId('chat-reasoning-body')).toBeNull();
  });
});
