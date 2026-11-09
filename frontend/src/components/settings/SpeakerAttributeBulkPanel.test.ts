import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$lib/axios', async () => (await import('$testMocks/serviceMocks')).axiosInstanceMock());

vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (
      run: (value: (key: string, params?: Record<string, unknown>) => string) => void
    ) => (run((k, params) => (params ? `${k}:${JSON.stringify(params)}` : k)), () => {}),
  },
}));

import axiosInstance from '$lib/axios';
import SpeakerAttributeBulkPanel from './SpeakerAttributeBulkPanel.svelte';

describe('SpeakerAttributeBulkPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it('requests migration status and offers the run button when files are pending', async () => {
    vi.mocked(axiosInstance.get).mockResolvedValue({
      data: { pending_files: 3, total_files: 10, progress: null },
    });
    render(SpeakerAttributeBulkPanel);
    expect(
      await screen.findByRole('button', { name: 'settings.speakerAttributes.runDetection' })
    ).toBeEnabled();
    expect(axiosInstance.get).toHaveBeenCalledWith('/speaker-attributes/migration/status');
  });
});
