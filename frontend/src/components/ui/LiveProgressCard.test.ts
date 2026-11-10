import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import LiveProgressCard from './LiveProgressCard.svelte';

function bar(container: HTMLElement): HTMLElement {
  const el = container.querySelector<HTMLElement>('[role="progressbar"]');
  if (!el) throw new Error(`no progressbar: ${container.innerHTML}`);
  return el;
}

describe('LiveProgressCard', () => {
  it('names the bar by barLabel and exposes the determinate value', () => {
    const { container } = render(LiveProgressCard, {
      props: { barLabel: 'Search indexing progress', done: 5, total: 20, percent: 25 },
    });
    const el = bar(container);
    expect(el.getAttribute('aria-label')).toBe('Search indexing progress');
    expect(el.getAttribute('aria-valuenow')).toBe('25');
    expect(container.querySelector('[aria-live="polite"]')?.textContent).toContain('5 / 20');
    expect(container.querySelector('[aria-live="polite"]')?.textContent).toContain('25%');
  });

  it('is indeterminate while starting, even if a percent is passed', () => {
    const { container } = render(LiveProgressCard, {
      props: { barLabel: 'X', state: 'starting', percent: 50, message: 'queued' },
    });
    expect(bar(container).hasAttribute('aria-valuenow')).toBe(false);
    expect(container.textContent).toContain('queued');
  });

  it('shows the ETA with its label only when there is one', async () => {
    const { container, rerender } = render(LiveProgressCard, {
      props: { barLabel: 'X', percent: 10, etaSeconds: 125, remainingLabel: 'remaining' },
    });
    expect(container.querySelector('.lpc-eta')?.textContent).toBe('2m 5s remaining');
    await rerender({ barLabel: 'X', percent: 10, etaSeconds: null, remainingLabel: 'remaining' });
    expect(container.querySelector('.lpc-eta')).toBeNull();
  });

  it('renders no stop button without onStop, and a danger-outline small one with it', async () => {
    const none = render(LiveProgressCard, { props: { barLabel: 'X' } });
    expect(none.container.querySelector('button')).toBeNull();
    const onStop = vi.fn();
    const { getByRole } = render(LiveProgressCard, {
      props: { barLabel: 'X', onStop, stopLabel: 'Stop' },
    });
    const btn = getByRole('button', { name: 'Stop' });
    expect(btn.className).toContain('btn-danger-outline');
    expect(btn.className).toContain('btn-sm');
    await fireEvent.click(btn);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it('busies the stop button and swallows clicks while stopping', async () => {
    const onStop = vi.fn();
    const { getByRole } = render(LiveProgressCard, {
      props: {
        barLabel: 'X',
        state: 'stopping',
        onStop,
        stopLabel: 'Stop',
        stoppingLabel: 'Stopping...',
      },
    });
    const btn = getByRole('button', { name: /Stopping/ });
    expect(btn.getAttribute('aria-busy')).toBe('true');
    await fireEvent.click(btn);
    expect(onStop).not.toHaveBeenCalled();
  });
});
