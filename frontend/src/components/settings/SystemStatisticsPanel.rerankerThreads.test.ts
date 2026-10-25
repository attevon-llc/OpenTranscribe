import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  // Identity translator so assertions can match on the i18n key.
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
}));

import SystemStatisticsPanel from './SystemStatisticsPanel.svelte';

function renderWith(chatRerankerThreads: unknown) {
  const system: Record<string, unknown> = {};
  if (chatRerankerThreads !== undefined) system.chat_reranker_threads = chatRerankerThreads;
  return render(SystemStatisticsPanel, {
    stats: { models: { whisper: { name: 'base' } }, system },
    statsLoading: false,
  });
}

describe('SystemStatisticsPanel: reranker threads (issue #1270)', () => {
  it('shows an automatic cap with the read-only help text', () => {
    renderWith({ threads: 8, source: 'auto', setting: 'auto' });

    expect(screen.getByTestId('reranker-threads')).toHaveTextContent(
      'settings.statistics.rerankerThreads'
    );
    expect(screen.getByTestId('reranker-threads')).toHaveTextContent(
      'settings.statistics.rerankerThreadsAuto'
    );
    expect(screen.getByText('settings.statistics.rerankerThreadsHelp')).toBeInTheDocument();
  });

  it('distinguishes an operator-set value from an automatic one', () => {
    renderWith({ threads: 3, source: 'configured', setting: '3' });

    expect(screen.getByTestId('reranker-threads')).toHaveTextContent(
      'settings.statistics.rerankerThreadsSet'
    );
    expect(screen.getByTestId('reranker-threads')).not.toHaveTextContent(
      'settings.statistics.rerankerThreadsAuto'
    );
  });

  it('says torch environment variables govern when no cap is applied', () => {
    renderWith({ threads: null, source: 'torch_env', setting: 'auto' });

    expect(screen.getByTestId('reranker-threads')).toHaveTextContent(
      'settings.statistics.rerankerThreadsTorchEnv'
    );
  });

  it('renders no row when the server does not report the field', () => {
    renderWith(undefined);

    expect(screen.queryByTestId('reranker-threads')).toBeNull();
  });

  it('offers no control to change it', () => {
    renderWith({ threads: 8, source: 'auto', setting: 'auto' });

    const row = screen.getByTestId('reranker-threads');
    expect(row.querySelector('input, select, button')).toBeNull();
  });
});
