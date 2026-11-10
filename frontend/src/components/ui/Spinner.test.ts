import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import Spinner from './Spinner.svelte';

describe('Spinner', () => {
  it('is hidden from assistive tech when text sits beside it', () => {
    const { container } = render(Spinner);
    const wrap = container.firstElementChild as HTMLElement;
    expect(wrap.getAttribute('aria-hidden')).toBe('true');
    expect(wrap.getAttribute('role')).toBeNull();
    expect(container.querySelector('.sr-only')).toBeNull();
  });

  it('becomes a named status region when given a label', () => {
    render(Spinner, { props: { label: 'Loading transcript' } });
    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Loading transcript');
    expect(status.getAttribute('aria-hidden')).toBeNull();
  });

  it.each(['small', 'inline', 'medium', 'large'] as const)('renders the %s size', (size) => {
    const { container } = render(Spinner, { props: { size } });
    const arc = container.querySelector('.spinner');
    expect(arc?.classList.contains(`spinner-${size}`)).toBe(true);
  });

  it('applies a custom colour to the visible arc', () => {
    const { container } = render(Spinner, { props: { color: 'white' } });
    const arc = container.querySelector<HTMLElement>('.spinner');
    expect(arc?.style.borderTopColor).toBe('white');
  });
});
