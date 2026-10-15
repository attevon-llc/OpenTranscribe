/**
 * Owner review (#1244 6c/6d): the date range picker must be an overlay anchored to its
 * trigger, not something that expands the card it sits in. These cover the open/close
 * contract; the geometry (flip/clamp/sheet) is in `$lib/popoverPosition.test.ts`.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, waitFor, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('@svelte-plugins/datepicker', () => ({
  DatePicker: () => ({ $set: () => {}, $destroy: () => {} }),
}));

import DateRangePicker from './DateRangePicker.svelte';

const layer = () => document.body.querySelector('.drp-layer') as HTMLElement | null;

async function open() {
  await fireEvent.click(screen.getByRole('button', { expanded: false }));
  await waitFor(() => expect(layer()).not.toBeNull());
  return document.body.querySelector('input[type="date"]') as HTMLInputElement;
}

describe('DateRangePicker popover', () => {
  it('opens a layer portaled to <body>, so it cannot grow or be clipped by its card', async () => {
    const { container } = render(DateRangePicker, { props: {} });
    expect(layer()).toBeNull();
    await open();
    expect(layer()!.parentElement).toBe(document.body);
    expect(container.contains(layer())).toBe(false);
    expect(layer()!.getAttribute('role')).toBe('dialog');
  });

  it('reflects open state on the trigger for assistive tech', async () => {
    render(DateRangePicker, { props: {} });
    const trigger = screen.getByRole('button', { expanded: false });
    expect(trigger.getAttribute('aria-haspopup')).toBe('dialog');
    await fireEvent.click(trigger);
    await waitFor(() => expect(trigger.getAttribute('aria-expanded')).toBe('true'));
  });

  it('closes on Escape and returns focus to the trigger', async () => {
    render(DateRangePicker, { props: {} });
    const trigger = screen.getByRole('button', { expanded: false });
    const fromInput = await open();
    await fireEvent.keyDown(fromInput, { key: 'Escape' });
    await waitFor(() => expect(layer()).toBeNull());
    expect(document.activeElement).toBe(trigger);
  });

  it('closes on an outside click', async () => {
    render(DateRangePicker, { props: {} });
    await open();
    await fireEvent.click(document.body);
    await waitFor(() => expect(layer()).toBeNull());
  });

  it('stays open when the click lands inside the layer', async () => {
    render(DateRangePicker, { props: {} });
    const fromInput = await open();
    await fireEvent.click(fromInput);
    expect(layer()).not.toBeNull();
  });
});
