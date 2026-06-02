import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import Chip from './Chip.svelte';

describe('Chip', () => {
  it('renders content and applies the variant class', () => {
    const { container } = render(Chip, { props: { variant: 'success' } });
    expect(container.querySelector('.chip')).toHaveClass('chip-success');
  });

  it('has no remove button when not removable', () => {
    render(Chip, { props: { removeLabel: 'Remove tag' } });
    expect(screen.queryByRole('button', { name: 'Remove tag' })).toBeNull();
  });

  it('renders a remove button and dispatches `remove` on click', async () => {
    const onRemove = vi.fn();
    render(Chip, {
      props: { removable: true, removeLabel: 'Remove tag' },
      events: { remove: onRemove },
    });

    const btn = screen.getByRole('button', { name: 'Remove tag' });
    expect(btn).toBeInTheDocument();
    await fireEvent.click(btn);
    expect(onRemove).toHaveBeenCalledTimes(1);
  });
});
