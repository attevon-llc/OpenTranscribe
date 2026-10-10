/**
 * Issue #750 item 6 — the picker's only affordance used to be a trigger
 * button that opens an inline calendar; there was no keyboard/text entry
 * path at all. These tests cover the two `type="date"` fields added above
 * the trigger, not the calendar plugin itself (which is stubbed out, as in
 * `FilterSidebar.test.ts`, since it is a heavy third-party widget not under
 * test here).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

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

describe('DateRangePicker — typed date fields (#750)', () => {
  it('renders empty typed fields when no range is set', () => {
    const { container } = render(DateRangePicker, { props: {} });
    const [fromInput, toInput] = Array.from(
      container.querySelectorAll('input[type="date"]')
    ) as HTMLInputElement[];

    expect(fromInput.value).toBe('');
    expect(toInput.value).toBe('');
  });

  it('reflects an existing range in both typed fields', () => {
    const { container } = render(DateRangePicker, {
      props: { from: new Date(2026, 0, 5), to: new Date(2026, 0, 20) },
    });
    const [fromInput, toInput] = Array.from(
      container.querySelectorAll('input[type="date"]')
    ) as HTMLInputElement[];

    expect(fromInput.value).toBe('2026-01-05');
    expect(toInput.value).toBe('2026-01-20');
  });

  it('typing a from-date dispatches change with a parsed Date', async () => {
    const onChange = vi.fn();
    const { container } = render(DateRangePicker, {
      props: {},
      events: { change: onChange },
    });
    const [fromInput] = Array.from(
      container.querySelectorAll('input[type="date"]')
    ) as HTMLInputElement[];

    await fireEvent.change(fromInput, { target: { value: '2026-03-01' } });

    await waitFor(() => expect(onChange).toHaveBeenCalled());
    const detail = onChange.mock.calls[0][0].detail;
    expect(detail.from.getFullYear()).toBe(2026);
    expect(detail.from.getMonth()).toBe(2);
    expect(detail.from.getDate()).toBe(1);
    expect(detail.to).toBeNull();
  });

  it('clearing a typed field dispatches null for that end', async () => {
    const onChange = vi.fn();
    const { container } = render(DateRangePicker, {
      props: { from: new Date(2026, 0, 5) },
      events: { change: onChange },
    });
    const [fromInput] = Array.from(
      container.querySelectorAll('input[type="date"]')
    ) as HTMLInputElement[];

    await fireEvent.change(fromInput, { target: { value: '' } });

    await waitFor(() => expect(onChange).toHaveBeenCalled());
    expect(onChange.mock.calls[0][0].detail.from).toBeNull();
  });

  it('caps the to-field at today when future dates are disabled', () => {
    const { container } = render(DateRangePicker, {
      props: { enableFutureDates: false },
    });
    const [, toInput] = Array.from(
      container.querySelectorAll('input[type="date"]')
    ) as HTMLInputElement[];
    const today = new Date();
    const pad = (n: number) => String(n).padStart(2, '0');
    const expected = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;

    expect(toInput.max).toBe(expected);
  });

  it('places no max on the to-field when future dates are allowed (the default)', () => {
    const { container } = render(DateRangePicker, { props: {} });
    const [, toInput] = Array.from(
      container.querySelectorAll('input[type="date"]')
    ) as HTMLInputElement[];

    expect(toInput.max).toBe('');
  });
});
