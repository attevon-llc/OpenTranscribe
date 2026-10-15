import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import SettingsSkeleton from './SettingsSkeleton.svelte';

describe('SettingsSkeleton', () => {
  it('draws label + field rows for a form, all on the shared shimmer', () => {
    const { container } = render(SettingsSkeleton, { props: { variant: 'form', count: 4 } });
    expect(container.querySelectorAll('.sk-field')).toHaveLength(4);
    expect(container.querySelectorAll('.sk-field .sk-label.shimmer')).toHaveLength(4);
    expect(container.querySelectorAll('.sk-field .sk-input.shimmer')).toHaveLength(4);
  });

  it('draws label + toggle rows for a preference list', () => {
    const { container } = render(SettingsSkeleton, { props: { variant: 'rows', count: 3 } });
    expect(container.querySelectorAll('.sk-toggle-row')).toHaveLength(3);
    expect(container.querySelectorAll('.sk-toggle.shimmer')).toHaveLength(3);
  });

  it('draws only the chip row for chips, and chips plus cards for a dashboard', () => {
    const chips = render(SettingsSkeleton, { props: { variant: 'chips' } });
    expect(chips.container.querySelectorAll('.sk-chip')).toHaveLength(3);
    expect(chips.container.querySelectorAll('.sk-card')).toHaveLength(0);
    chips.unmount();

    const dash = render(SettingsSkeleton, { props: { variant: 'dashboard', count: 5 } });
    expect(dash.container.querySelectorAll('.sk-chip')).toHaveLength(3);
    // Two cards at most, however many rows the caller asked for.
    expect(dash.container.querySelectorAll('.sk-card')).toHaveLength(2);
  });

  it('is a named status region only when given a label', () => {
    const { rerender } = render(SettingsSkeleton, { props: { variant: 'form' } });
    expect(screen.queryByRole('status')).toBeNull();
    rerender({ label: 'Loading configuration' });
    expect(screen.getByRole('status', { name: 'Loading configuration' })).toHaveAttribute(
      'aria-busy',
      'true'
    );
  });

  it('always draws at least one row', () => {
    const { container } = render(SettingsSkeleton, { props: { variant: 'form', count: 0 } });
    expect(container.querySelectorAll('.sk-field')).toHaveLength(1);
  });
});
