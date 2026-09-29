import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

/**
 * In an organization every member is listed the org's collections (issue #1051),
 * but only an owner (the creator or an org admin) may delete one. The list
 * renders the backend's `my_permission` verdict — it decides nothing itself.
 */
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key) };
});

import CollectionsList from './CollectionsList.svelte';
import type { Collection } from '$lib/types/collection';

const owned: Collection = { uuid: 'c1', name: 'Board', media_count: 2, my_permission: 'owner' };
const colleagues: Collection = {
  uuid: 'c2',
  name: 'Customer Calls',
  media_count: 5,
  my_permission: 'editor',
};

function deleteButtons(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('.delete-config-button'));
}

describe('CollectionsList — manage actions follow my_permission', () => {
  it('offers delete on a collection the caller owns', () => {
    render(CollectionsList, { props: { collections: [owned] } });
    expect(deleteButtons()).toHaveLength(1);
  });

  it("hides delete on a colleague's organization collection, keeping edit", () => {
    render(CollectionsList, { props: { collections: [colleagues] } });
    expect(deleteButtons()).toHaveLength(0);
    expect(document.querySelectorAll('.edit-button')).toHaveLength(1);
  });

  it('treats a missing my_permission as owner (older responses)', () => {
    const legacy: Collection = { uuid: 'c3', name: 'Legacy', media_count: 0 };
    render(CollectionsList, { props: { collections: [legacy] } });
    expect(deleteButtons()).toHaveLength(1);
  });
});
