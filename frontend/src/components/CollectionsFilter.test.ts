/**
 * The collections facet of the gallery's filter sidebar. A failed load used to fall through to
 * "no collections created", which a user cannot tell apart from having none; it now shows an
 * error with a retry, like the other facets (#743a). A request cancelled at sign-out is not a
 * failure.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

const mockAxios = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/axios')>()),
  default: mockAxios,
}));
vi.mock('$lib/apiCache', () => ({
  apiCache: { getOrFetch: (_key: string, fetchFn: () => unknown) => fetchFn() },
  cacheKey: { collections: () => 'collections' },
  CacheTTL: { COLLECTIONS: 0 },
}));

import CollectionsFilter from './CollectionsFilter.svelte';

beforeEach(() => vi.clearAllMocks());

describe('CollectionsFilter load failures', () => {
  it('shows an error with a retry, not "no collections created", and the retry recovers', async () => {
    mockAxios.get.mockRejectedValueOnce(new Error('Request failed with status code 500'));
    mockAxios.get.mockResolvedValueOnce({
      data: [{ uuid: 'c1', name: 'Board meetings', media_count: 2 }],
    });
    const { container } = render(CollectionsFilter);

    const retry = (await waitFor(() => {
      const el = container.querySelector('[data-testid="collections-retry"]');
      expect(el).not.toBeNull();
      return el;
    })) as HTMLElement;
    expect(container.textContent).toContain('filter.collectionsLoadFailed');
    expect(container.textContent).not.toContain('collections.noCollectionsCreated');

    await fireEvent.click(retry);
    await waitFor(() =>
      expect(container.querySelector('[data-testid="collections-retry"]')).toBeNull()
    );
    expect(container.textContent).not.toContain('collections.noCollectionsCreated');
    expect(mockAxios.get).toHaveBeenCalledTimes(2);
  });

  it('a request cancelled at sign-out shows no error', async () => {
    mockAxios.get.mockRejectedValueOnce(
      Object.assign(new Error('canceled'), { code: 'ERR_CANCELED', name: 'CanceledError' })
    );
    const { container } = render(CollectionsFilter);
    await waitFor(() =>
      expect(container.textContent).not.toContain('collections.loadingCollections')
    );
    expect(container.querySelector('[data-testid="collections-retry"]')).toBeNull();
  });
});
