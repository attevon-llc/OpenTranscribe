import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, string>) => string) => void) => {
      run((key, vars) => (vars ? `${key}|${vars.value}` : key));
      return () => {};
    },
  },
}));

import RangeSlider from './RangeSlider.svelte';
import RangeSliderTestHost from './RangeSliderTestHost.svelte';

function input(): HTMLInputElement {
  return screen.getByRole('slider') as HTMLInputElement;
}

const base = {
  id: 'tox',
  label: 'Toxicity flag threshold',
  value: 0.5,
  min: 0,
  max: 1,
  step: 0.05,
};

describe('RangeSlider', () => {
  it('renders a native range input labelled through for/id, with the formatted badge', () => {
    const { container } = render(RangeSlider, { props: base });
    const el = input();
    expect(el.type).toBe('range');
    expect(el.id).toBe('tox');
    expect(el.classList.contains('range-input')).toBe(true);
    expect(screen.getByLabelText(/Toxicity flag threshold/)).toBe(el);
    expect(container.querySelector('.range-slider__badge')?.textContent).toBe('0.50');
  });

  it('honours decimals and unit', () => {
    const { container } = render(RangeSlider, {
      props: { ...base, value: 1500, min: 100, max: 5000, step: 100, decimals: 0, unit: ' ms' },
    });
    expect(container.querySelector('.range-slider__badge')?.textContent).toBe('1500 ms');
  });

  it('lets a formatter and an explicit valueText take over the badge', () => {
    const { container, rerender } = render(RangeSlider, {
      props: { ...base, value: 0.75, format: (v: number) => `${Math.round(v * 100)}%` },
    });
    const badge = () => container.querySelector('.range-slider__badge')?.textContent;
    expect(badge()).toBe('75%');
    rerender({ valueText: 'inherit' });
    expect(badge()).toBe('inherit');
  });

  it('updates its badge live while the native input changes', async () => {
    const { container } = render(RangeSlider, { props: base });
    await fireEvent.input(input(), { target: { value: '0.8' } });
    expect(container.querySelector('.range-slider__badge')?.textContent).toBe('0.80');
  });

  it('forwards component-level on:input and on:change with the native input as target', async () => {
    render(RangeSliderTestHost);
    await fireEvent.input(input(), { target: { value: '0.25' } });
    expect(screen.getByTestId('host-input').textContent).toBe('0.25');
    expect(screen.getByTestId('host-change').textContent).toBe('');
    await fireEvent.change(input(), { target: { value: '0.35' } });
    expect(screen.getByTestId('host-change').textContent).toBe('0.35');
  });

  it('supports bind:value in both directions', async () => {
    render(RangeSliderTestHost);
    await fireEvent.input(input(), { target: { value: '0.7' } });
    expect(screen.getByTestId('host-value').textContent).toBe('0.7');
    await fireEvent.click(screen.getByTestId('host-set'));
    expect(Number(input().value)).toBeCloseTo(0.9);
  });

  it('disables the native input', () => {
    render(RangeSlider, { props: { ...base, disabled: true } });
    expect(input()).toBeDisabled();
  });

  it('keeps native keyboard semantics: focusable, and no key is swallowed', async () => {
    render(RangeSlider, { props: base });
    const el = input();
    el.focus();
    expect(document.activeElement).toBe(el);
    expect([el.min, el.max, el.step]).toEqual(['0', '1', '0.05']);
    // jsdom does not step ranges, so assert the contract that matters here: the
    // component never cancels the keys the browser uses to move a native slider.
    for (const key of [
      'ArrowLeft',
      'ArrowRight',
      'ArrowUp',
      'ArrowDown',
      'PageUp',
      'PageDown',
      'Home',
      'End',
    ]) {
      expect(await fireEvent.keyDown(el, { key }), key).toBe(true);
    }
  });

  it('shows hint and the system-default line only when given', () => {
    const { container, rerender } = render(RangeSlider, { props: base });
    expect(container.querySelector('.range-slider__hint')).toBeNull();
    expect(container.querySelector('.range-slider__default')).toBeNull();
    rerender({ hint: 'Higher = stricter', defaultValue: 0.3 });
    expect(container.querySelector('.range-slider__hint')?.textContent).toBe('Higher = stricter');
    expect(container.querySelector('.range-slider__default')?.textContent?.trim()).toBe(
      'ui.rangeSlider.systemDefault|0.30'
    );
  });

  it('passes data-*, aria-* and dir through to the native input (RTL, test ids)', () => {
    render(RangeSlider, {
      props: { ...base, dir: 'rtl', 'data-testid': 'chat-temperature', 'aria-describedby': 'h' },
    } as never);
    const el = input();
    expect(el.getAttribute('dir')).toBe('rtl');
    expect(el.getAttribute('data-testid')).toBe('chat-temperature');
    expect(el.getAttribute('aria-describedby')).toBe('h');
  });

  it('uses theme tokens only: no hard-coded colour in the component', async () => {
    const { readFileSync } = await import('fs');
    const { resolve } = await import('path');
    const src = readFileSync(resolve(__dirname, 'RangeSlider.svelte'), 'utf8');
    const style = src.slice(src.indexOf('<style>'));
    expect(style).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgb\(|rgba\(\s*\d/);
  });
});
