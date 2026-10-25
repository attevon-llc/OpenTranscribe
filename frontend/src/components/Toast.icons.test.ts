/**
 * DEFECT THESE CATCH: success and error toasts drew their icon as a text glyph (a check mark and
 * a ballot X) through `{@html}`, while warning and info drew SVG, and info drew a CHECK in a
 * circle, so an info toast read as a success. Each type now has its own named SVG mark.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import Toast from './Toast.svelte';

afterEach(() => cleanup());

describe('Toast icons', () => {
  it.each([
    ['success', 'check'],
    ['error', 'x'],
    ['warning', 'exclamation'],
    ['info', 'info-mark'],
  ] as const)('draws a %s toast with the %s SVG and no glyph text', (type, icon) => {
    const { container } = render(Toast, { props: { message: 'Saved', type, duration: 0 } });
    const holder = container.querySelector('.toast-icon');

    expect(holder?.querySelector('svg')?.getAttribute('data-icon')).toBe(icon);
    expect(holder?.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true');
    expect(holder?.textContent?.trim()).toBe('');
  });
});
