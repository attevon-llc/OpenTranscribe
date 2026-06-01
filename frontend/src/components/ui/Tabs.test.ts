import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import Tabs from './Tabs.svelte';

const tabs = [
  { id: 'a', label: 'Alpha' },
  { id: 'b', label: 'Beta', badge: 3 },
  { id: 'c', label: 'Gamma' },
];

describe('Tabs', () => {
  it('renders tabs, marks the active one, and shows badges', () => {
    render(Tabs, { props: { tabs, activeId: 'a', ariaLabel: 'Sections' } });
    expect(screen.getByRole('tablist', { name: 'Sections' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Alpha/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: /Beta/ })).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('activates a tab on click', async () => {
    render(Tabs, { props: { tabs, activeId: 'a' } });
    await fireEvent.click(screen.getByRole('tab', { name: /Beta/ }));
    expect(screen.getByRole('tab', { name: /Beta/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: /Alpha/ })).toHaveAttribute('aria-selected', 'false');
  });

  it('moves selection with arrow keys', async () => {
    render(Tabs, { props: { tabs, activeId: 'a' } });
    await fireEvent.keyDown(screen.getByRole('tab', { name: /Alpha/ }), { key: 'ArrowRight' });
    expect(screen.getByRole('tab', { name: /Beta/ })).toHaveAttribute('aria-selected', 'true');
  });
});
