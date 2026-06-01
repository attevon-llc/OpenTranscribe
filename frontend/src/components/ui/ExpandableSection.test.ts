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
});
