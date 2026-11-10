import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import ProgressBar from './ProgressBar.svelte';

function bar(container: HTMLElement): HTMLElement {
  const el = container.querySelector<HTMLElement>('[role="progressbar"]');
  if (!el) throw new Error(`no progressbar: ${container.innerHTML}`);
  return el;
}

describe('ProgressBar', () => {
  it('exposes determinate value per the WAI-ARIA progressbar pattern', () => {
    const { container } = render(ProgressBar, { props: { percent: 42.4, label: 'Upload' } });
    const el = bar(container);
    expect(el.getAttribute('aria-valuemin')).toBe('0');
    expect(el.getAttribute('aria-valuemax')).toBe('100');
    expect(el.getAttribute('aria-valuenow')).toBe('42');
    expect(el.getAttribute('aria-valuetext')).toBe('42%');
    expect(el.getAttribute('aria-label')).toBe('Upload');
  });

  it('omits aria-valuenow when indeterminate', () => {
    const { container } = render(ProgressBar, { props: { percent: null, ariaLabel: 'Loading' } });
    const el = bar(container);
    expect(el.hasAttribute('aria-valuenow')).toBe(false);
    expect(el.hasAttribute('aria-valuetext')).toBe(false);
    expect(el.getAttribute('aria-label')).toBe('Loading');
    expect(container.querySelector('.indeterminate')).not.toBeNull();
  });

  it('clamps out-of-range values', () => {
    const over = render(ProgressBar, { props: { percent: 180, ariaLabel: 'x' } });
    expect(bar(over.container).getAttribute('aria-valuenow')).toBe('100');
    expect(over.container.querySelector<HTMLElement>('.progress-fill')!.style.width).toBe('100%');
    const under = render(ProgressBar, { props: { percent: -5, ariaLabel: 'x' } });
    expect(bar(under.container).getAttribute('aria-valuenow')).toBe('0');
  });

  it('prefers ariaLabel over the visible label', () => {
    const { container } = render(ProgressBar, {
      props: { percent: 1, label: 'Visible', ariaLabel: 'Explicit' },
    });
    expect(bar(container).getAttribute('aria-label')).toBe('Explicit');
  });

  it('defaults to primary and applies exactly the requested tone', () => {
    const d = render(ProgressBar, { props: { percent: 10, ariaLabel: 'x' } });
    expect(
      [...d.container.querySelector('.progress-fill')!.classList].filter((c) =>
        c.startsWith('tone-')
      )
    ).toEqual(['tone-primary']);
    const e = render(ProgressBar, { props: { percent: 10, ariaLabel: 'x', tone: 'error' } });
    expect(
      [...e.container.querySelector('.progress-fill')!.classList].filter((c) =>
        c.startsWith('tone-')
      )
    ).toEqual(['tone-error']);
  });

  it('warns in development when it has no accessible name, and not when named', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    render(ProgressBar, { props: { percent: 5 } });
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toContain('ProgressBar');
    warn.mockClear();
    render(ProgressBar, { props: { percent: 5, ariaLabel: 'Named' } });
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it('keeps the label row at --font-size-md (0.875rem), the largest size any caller used before', async () => {
    const fs = await import('node:fs');
    const path = await import('node:path');
    const src = fs.readFileSync(path.resolve(__dirname, 'ProgressBar.svelte'), 'utf8');
    const rule = src.match(/\.progress-label\s*\{[^}]*\}/)![0];
    expect(rule).toMatch(/font-size:\s*var\(--font-size-md\)/);
  });
});
