/**
 * An empty Clusters tab used to tell a user who had just uploaded a file to "upload and
 * transcribe media files, then click Re-cluster All" — while the speakers from that very
 * upload sat unmatched under the Review tab. Re-cluster All builds groups of two or more, so
 * lone speakers legitimately end up there; the empty state has to say so and point at them.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import ClustersTab from './ClustersTab.svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string, vars?: Record<string, unknown>) =>
        vars ? `${key}:${JSON.stringify(vars)}` : key
      );
      return () => {};
    },
  },
}));

const baseProps = { clusterSearch: '', clusters: [], loadingClusters: false };

describe('ClustersTab empty state', () => {
  it('points at Review when speakers are waiting there', async () => {
    const opened = vi.fn();
    render(ClustersTab, {
      props: { ...baseProps, reviewCount: 4 },
      events: { openReview: opened },
    } as never);

    expect(screen.getByText('speakers.clusters.emptyReviewDesc:{"count":4}')).toBeInTheDocument();
    expect(screen.queryByText('speakers.clusters.emptyDesc')).not.toBeInTheDocument();

    await fireEvent.click(screen.getByRole('button', { name: 'speakers.clusters.openReview' }));
    expect(opened).toHaveBeenCalledTimes(1);
  });

  it('keeps the upload-and-recluster hint when nothing is waiting in Review', () => {
    render(ClustersTab, { props: { ...baseProps, reviewCount: 0 } });

    expect(screen.getByText('speakers.clusters.emptyDesc')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'speakers.clusters.openReview' })
    ).not.toBeInTheDocument();
  });
});
