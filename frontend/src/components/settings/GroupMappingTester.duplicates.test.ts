import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());

const test = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/groupMappings', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api/groupMappings')>()),
  GroupMappingsApi: { test },
}));

import GroupMappingTester from './GroupMappingTester.svelte';

describe('GroupMappingTester with repeated claim values', () => {
  it('renders every chip when a pasted list repeats a value', async () => {
    test.mockResolvedValue({
      source: 'oidc',
      claim_values: ['Legal', 'Legal', 'Ops', 'Ops'],
      matched_claims: ['Legal', 'Legal'],
      unmatched_claims: ['Ops', 'Ops'],
      groups: [],
      grants_role: null,
      legacy_admin: false,
      effective_role: 'user',
    });
    const { container } = render(GroupMappingTester, { props: { source: 'oidc' } });
    await fireEvent.input(container.querySelector('#mapping-test-claims')!, {
      target: { value: 'Legal\nLegal\nOps\nOps' },
    });
    await fireEvent.click(container.querySelector('.tester-actions button') as HTMLElement);

    await waitFor(() => expect(container.querySelector('.result')).not.toBeNull());
    const chips = Array.from(container.querySelectorAll('.result .chips')).map((c) =>
      Array.from(c.children).map((badge) => badge.textContent?.trim())
    );
    expect(chips).toContainEqual(['Legal', 'Legal']);
    expect(chips).toContainEqual(['Ops', 'Ops']);
  });
});
