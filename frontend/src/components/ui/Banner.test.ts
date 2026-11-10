import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import Banner from './Banner.svelte';

describe('Banner live-region semantics', () => {
  it('uses role=alert for an error', () => {
    render(Banner, { props: { tone: 'error' } });
    expect(screen.getByRole('alert')).toBeTruthy();
  });

  it.each(['info', 'success', 'warning', 'neutral'] as const)(
    'uses role=status, not alert, for %s',
    (tone) => {
      render(Banner, { props: { tone } });
      expect(screen.getByRole('status')).toBeTruthy();
      expect(screen.queryByRole('alert')).toBeNull();
    }
  );

  it('renders no live role when live is off', () => {
    render(Banner, { props: { tone: 'error', live: 'off' } });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('lets the caller force assertive or polite', () => {
    const a = render(Banner, { props: { tone: 'info', live: 'assertive' } });
    expect(screen.getByRole('alert')).toBeTruthy();
    a.unmount();
    render(Banner, { props: { tone: 'error', live: 'polite' } });
    expect(screen.getByRole('status')).toBeTruthy();
  });
});

describe('Banner structure', () => {
  it('exposes a test id and the spacing switch', () => {
    const { container } = render(Banner, { props: { testid: 'my-banner', spaced: false } });
    const root = container.querySelector<HTMLElement>('[data-testid="my-banner"]');
    expect(root).not.toBeNull();
    expect(root?.classList.contains('spaced')).toBe(false);
  });

  it('is spaced by default', () => {
    const { container } = render(Banner);
    expect(container.querySelector('.banner')?.classList.contains('spaced')).toBe(true);
  });

  it('gives each tone its own glyph, so the tone is not colour alone', () => {
    const glyphs = (['info', 'success', 'warning', 'error'] as const).map((tone) => {
      const { container, unmount } = render(Banner, { props: { tone } });
      const icon = container.querySelector('svg')?.getAttribute('data-icon');
      unmount();
      return icon;
    });
    expect(new Set(glyphs).size).toBe(4);
  });

  it('renders the tone class, title and body', () => {
    const { container } = render(Banner, { props: { tone: 'warning', title: 'Heads up' } });
    expect(container.querySelector('.banner-warning')).not.toBeNull();
    expect(screen.getByText('Heads up')).toBeTruthy();
  });

  it('omits the title and dismiss button by default', () => {
    const { container } = render(Banner);
    expect(container.querySelector('.banner-title')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('shows a named dismiss button that dispatches dismiss', async () => {
    const onDismiss = vi.fn();
    render(Banner, {
      props: { dismissible: true, dismissLabel: 'Hide it' },
      events: { dismiss: onDismiss },
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Hide it' }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('falls back to the translated Close name for the dismiss button', () => {
    render(Banner, { props: { dismissible: true } });
    expect(screen.getByRole('button').getAttribute('aria-label')).toBeTruthy();
  });
});

describe('Banner size', () => {
  it('defaults to md and offers the larger body size', () => {
    const a = render(Banner);
    expect(a.container.querySelector('.banner')?.classList.contains('size-md')).toBe(true);
    a.unmount();
    const b = render(Banner, { props: { size: 'body' } });
    const root = b.container.querySelector('.banner');
    expect(root?.classList.contains('size-body')).toBe(true);
    expect(root?.classList.contains('size-md')).toBe(false);
  });
});
