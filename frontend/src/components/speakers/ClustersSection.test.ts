/**
 * ClustersSection owns the Clusters tab's data and flows (split out of routes/speakers). These
 * pin the page-facing contract: `load` fills the bound total, a failed load toasts unless
 * silent, and Escape reports whether a mode was active.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((key: string) => key), locale: readable('en') };
});
const stub = vi.hoisted(() => async () => ({
  default: (await import('$testMocks/EmptyComponent.svelte')).default,
}));
vi.mock('$components/ConfirmationModal.svelte', stub);
vi.mock('$components/ui/BaseModal.svelte', stub);
vi.mock('$components/speakers/ClustersTab.svelte', stub);

const api = vi.hoisted(() => ({
  listClusters: vi.fn(),
  getClusterDetail: vi.fn(),
  updateCluster: vi.fn(),
  deleteCluster: vi.fn(),
  promoteCluster: vi.fn(),
  mergeClusters: vi.fn(),
  splitCluster: vi.fn(),
  triggerRecluster: vi.fn(),
  unassignSpeakers: vi.fn(),
}));
vi.mock('$lib/api/speakerClusters', () => api);
const toast = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import ClustersSection from './ClustersSection.svelte';
import { apiCache } from '$lib/apiCache';

beforeEach(() => {
  vi.clearAllMocks();
  apiCache.clear();
});

describe('ClustersSection', () => {
  it('loads the first page with the page size and no search', async () => {
    api.listClusters.mockResolvedValue({ items: [], total: 7, pages: 1 });
    const { component } = render(ClustersSection, { props: { active: true } });
    await (component as unknown as { load: () => Promise<void> }).load();
    expect(api.listClusters).toHaveBeenCalledWith(1, 20, undefined);
  });

  it('toasts a failed load unless silent, and always rethrows', async () => {
    api.listClusters.mockRejectedValue(new Error('boom'));
    const { component } = render(ClustersSection, { props: { active: true } });
    const load = (component as unknown as { load: (s?: boolean) => Promise<void> }).load;
    await expect(load(true)).rejects.toThrow('boom');
    expect(toast.error).not.toHaveBeenCalled();
    await expect(load()).rejects.toThrow('boom');
    expect(toast.error).toHaveBeenCalledWith('speakers.error.loadClusters');
  });

  it('refresh (tab switch, pagination) toasts a failed load and settles without rejecting', async () => {
    api.listClusters.mockRejectedValue(new Error('boom'));
    const { component } = render(ClustersSection, { props: { active: true } });
    const refresh = (component as unknown as { refresh: () => void }).refresh;
    const unhandled: unknown[] = [];
    const onUnhandled = (reason: unknown) => unhandled.push(reason);
    process.on('unhandledRejection', onUnhandled);
    try {
      refresh();
      await vi.waitFor(() =>
        expect(toast.error).toHaveBeenCalledWith('speakers.error.loadClusters')
      );
      await new Promise((resolve) => setTimeout(resolve, 0));
    } finally {
      process.off('unhandledRejection', onUnhandled);
    }
    expect(unhandled).toEqual([]);
  });

  it('reports no active mode when idle', () => {
    const { component } = render(ClustersSection, { props: { active: false } });
    expect((component as unknown as { cancelActiveMode: () => boolean }).cancelActiveMode()).toBe(
      false
    );
  });
});
