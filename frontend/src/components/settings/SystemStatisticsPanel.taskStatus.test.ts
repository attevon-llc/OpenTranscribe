import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyLocale({ code: 'en' })
);

import SystemStatisticsPanel from './SystemStatisticsPanel.svelte';
import { initialSystemStats } from '$lib/settings/adminLoaders';

const identity = (key: string) => key;

function renderWithStatus(status: string) {
  const base = initialSystemStats(identity);
  const stats = {
    ...base,
    tasks: {
      ...base.tasks,
      recent: [
        {
          id: 'abcdef1234567890',
          type: 'summarization',
          status,
          created_at: '2026-01-01T00:00:00Z',
          elapsed: 3,
        },
      ],
    },
  } as typeof base;
  return render(SystemStatisticsPanel, { stats, statsLoading: false });
}

describe('SystemStatisticsPanel: recent task status labels', () => {
  it.each([
    ['skipped', 'notifications.job.state.skipped'],
    ['cancelled', 'common.cancelled'],
    ['revoked', 'common.cancelled'],
    ['in_progress', 'fileStatus.inProgress'],
  ])('translates %s through %s', (status, key) => {
    renderWithStatus(status);
    expect(screen.getByText(key)).toBeInTheDocument();
  });
});
