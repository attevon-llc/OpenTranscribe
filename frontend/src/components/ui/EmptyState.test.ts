import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import EmptyState from './EmptyState.svelte';

/**
 * Harness smoke test — proves the Svelte 5 + jsdom + @testing-library render path
 * works, so Phase 1 primitive tests are guaranteed a working environment.
 */
describe('EmptyState (harness smoke)', () => {
  it('renders title and description props', () => {
    render(EmptyState, { props: { title: 'No files', description: 'Upload to begin' } });
    expect(screen.getByRole('heading', { name: 'No files' })).toBeInTheDocument();
    expect(screen.getByText('Upload to begin')).toBeInTheDocument();
  });
});
