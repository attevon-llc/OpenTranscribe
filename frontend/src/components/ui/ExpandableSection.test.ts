import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import ExpandableSection from './ExpandableSection.svelte';

describe('ExpandableSection', () => {
  it('is collapsed initially and expands on click', async () => {
    render(ExpandableSection, { props: { title: 'Details' } });
    const btn = screen.getByRole('button', { name: /Details/ });
    expect(btn).toHaveAttribute('aria-expanded', 'false');

    await fireEvent.click(btn);
    expect(btn).toHaveAttribute('aria-expanded', 'true');
  });

  it('keeps the toggle inside a heading when headingLevel is set', () => {
    render(ExpandableSection, { props: { title: 'Summaries', headingLevel: 2, expanded: true } });
    const heading = screen.getByRole('heading', { level: 2, name: 'Summaries' });
    const btn = screen.getByRole('button', { name: 'Summaries' });
    expect(heading).toContainElement(btn);
    expect(btn).toHaveAttribute('aria-expanded', 'true');
  });

  it('renders no heading by default', () => {
    render(ExpandableSection, { props: { title: 'Details' } });
    expect(screen.queryByRole('heading')).toBeNull();
  });
});
