import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import fs from 'fs';
import path from 'path';
import EmptyState from './EmptyState.svelte';
import EmptyStateActionTestHost from './EmptyStateActionTestHost.svelte';

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

/**
 * Issue #1243 item 16: the Speaker Clusters empty state put "Open Review" flush against
 * its description because each caller spaced (or forgot to space) its own action. The
 * action row is now owned by the primitive, so every empty state shares one rhythm.
 */
describe('EmptyState action row', () => {
  it('wraps slotted actions in the shared action row', () => {
    render(EmptyStateActionTestHost);
    const action = screen.getByRole('button', { name: 'Open Review' });
    expect(action.parentElement?.classList.contains('empty-actions')).toBe(true);
  });

  it('renders no action row when nothing is slotted', () => {
    const { container } = render(EmptyState, { props: { title: 'No files' } });
    expect(container.querySelector('.empty-actions')).toBeNull();
  });

  it('spaces the action row with the shared token, not a literal', () => {
    const src = fs.readFileSync(path.resolve(__dirname, 'EmptyState.svelte'), 'utf8');
    const rule = src.match(/\.empty-actions\s*\{([^}]*)\}/)?.[1] ?? '';
    expect(rule).toMatch(/margin-top:\s*var\(--empty-state-action-gap\)/);
  });
});
