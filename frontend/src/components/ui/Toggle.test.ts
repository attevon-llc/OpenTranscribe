import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import Toggle from './Toggle.svelte';

const sw = () => screen.getByRole('switch') as HTMLInputElement;
const props = (over = {}) => ({ checked: false, onchange: vi.fn(), label: 'Dark', ...over });

describe('Toggle ARIA', () => {
  it('is a switch named by label with aria-checked mirroring checked', () => {
    const { unmount } = render(Toggle, props({ checked: true }));
    expect(screen.getByRole('switch', { name: 'Dark' })).toBe(sw());
    expect(sw().getAttribute('aria-checked')).toBe('true');
    unmount();
    render(Toggle, props());
    expect(sw().getAttribute('aria-checked')).toBe('false');
  });

  it('wires labelledby and describedby', () => {
    render(Toggle, props({ label: undefined, labelledby: 't', describedby: 'd' }));
    expect(sw().getAttribute('aria-labelledby')).toBe('t');
    expect(sw().getAttribute('aria-describedby')).toBe('d');
  });
});

describe('Toggle behaviour', () => {
  it('asks for the opposite value on click and stays controlled', async () => {
    const p = props();
    render(Toggle, p);
    await fireEvent.click(sw());
    expect(p.onchange).toHaveBeenCalledExactlyOnceWith(true);
    expect(sw()).not.toBeChecked();
  });

  it('Space (native click activation) is supported by a focusable checkbox', () => {
    render(Toggle, props());
    sw().focus();
    expect(document.activeElement).toBe(sw());
    expect(sw().type).toBe('checkbox');
  });

  it('Enter toggles', async () => {
    const p = props({ checked: true });
    render(Toggle, p);
    await fireEvent.keyDown(sw(), { key: 'Enter' });
    expect(p.onchange).toHaveBeenCalledExactlyOnceWith(false);
  });

  it('ignores a change when disabled', async () => {
    const p = props({ disabled: true });
    render(Toggle, p);
    expect(sw()).toBeDisabled();
    await fireEvent.change(sw());
    expect(p.onchange).not.toHaveBeenCalled();
  });

  it('busy keeps focusability but ignores changes and Enter', async () => {
    const p = props({ busy: true });
    render(Toggle, p);
    expect(sw()).not.toBeDisabled();
    expect(sw().getAttribute('aria-disabled')).toBe('true');
    await fireEvent.change(sw());
    await fireEvent.keyDown(sw(), { key: 'Enter' });
    expect(p.onchange).not.toHaveBeenCalled();
  });
});

describe('Toggle RTL', () => {
  it('positions the knob with logical offsets only', async () => {
    const src = (await import('./Toggle.svelte?raw')).default as string;
    expect(src).toContain('inset-inline-start: 2px');
    expect(src).toContain('inset-inline-start: 22px');
    expect(src).not.toMatch(/\n\s*(left|right):/);
  });
});
