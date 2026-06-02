import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import Dropdown from './Dropdown.svelte';

describe('Dropdown', () => {
  it('is closed initially and opens on trigger click', async () => {
    render(Dropdown, { props: { ariaLabel: 'Sort' } });
    const trigger = screen.getByRole('button', { name: 'Sort' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('menu')).toBeNull();

    await fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('menu')).toBeInTheDocument();
  });

  it('closes on Escape', async () => {
    render(Dropdown, { props: { ariaLabel: 'Sort', open: true } });
    expect(screen.getByRole('menu')).toBeInTheDocument();
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('toggles closed when the trigger is clicked again', async () => {
    render(Dropdown, { props: { ariaLabel: 'Sort', open: true } });
    const trigger = screen.getByRole('button', { name: 'Sort' });
    await fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('menu')).toBeNull();
  });
});
