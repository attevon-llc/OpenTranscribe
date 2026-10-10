import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import FilterChipButton from './FilterChipButton.svelte';

describe('FilterChipButton', () => {
  it('renders unselected by default with aria-pressed=false', () => {
    render(FilterChipButton, { props: {} });
    const btn = screen.getByRole('button');
    expect(btn).toHaveAttribute('aria-pressed', 'false');
    expect(btn).not.toHaveClass('selected');
  });

  it('reflects `selected` as both the class and aria-pressed', () => {
    render(FilterChipButton, { props: { selected: true } });
    const btn = screen.getByRole('button');
    expect(btn).toHaveAttribute('aria-pressed', 'true');
    expect(btn).toHaveClass('selected');
  });

  it('renders a trailing count badge only when count is a real number', () => {
    const { container, rerender } = render(FilterChipButton, { props: { count: null } });
    expect(container.querySelector('.filter-chip-count')).toBeNull();

    rerender({ count: 7 });
    expect(container.querySelector('.filter-chip-count')?.textContent).toBe('7');
  });

  it('forwards a caller-supplied class alongside its own, for selector stability', () => {
    render(FilterChipButton, { props: { class: 'tag-button' } });
    const btn = screen.getByRole('button');
    expect(btn).toHaveClass('filter-chip-btn');
    expect(btn).toHaveClass('tag-button');
  });

  it('dispatches a click event to the consumer', async () => {
    const onClick = vi.fn();
    render(FilterChipButton, { props: {}, events: { click: onClick } });

    await fireEvent.click(screen.getByRole('button'));

    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('forwards arbitrary attributes (e.g. data-testid) to the button element', () => {
    render(FilterChipButton, { props: { 'data-testid': 'unlabeled-speakers-facet' } });
    expect(screen.getByTestId('unlabeled-speakers-facet')).toBeInTheDocument();
  });
});
