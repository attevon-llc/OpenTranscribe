/**
 * The review row names the transcription that will really run, in the same words as the Model
 * step (#1243): a saved cloud provider is not "High Quality (GPU)".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

const asrApi = vi.hoisted(() => ({ getProviders: vi.fn(), getStatus: vi.fn() }));
vi.mock('$lib/api/asrSettings', () => ({ ASRSettingsApi: asrApi }));
vi.mock('$stores/locale', () => {
  const t = (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}${JSON.stringify(vars)}` : key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});

import UploadStepReview from './UploadStepReview.svelte';

beforeEach(() => {
  vi.clearAllMocks();
  asrApi.getProviders.mockResolvedValue({
    providers: [
      {
        id: 'gladia',
        provider: 'gladia',
        display_name: 'Gladia',
        supports_diarization: true,
        speaker_count_hints: [],
        timestamp_granularity: 'word',
        models: [],
      },
    ],
  });
  asrApi.getStatus.mockResolvedValue({
    deployment_mode: 'full',
    is_cloud_provider: true,
    active_provider: 'gladia',
    active_model: 'solaria-1',
  });
});

describe('UploadStepReview model row', () => {
  it('names the saved cloud provider with its flags once the catalog has loaded', async () => {
    const { container } = render(UploadStepReview, { props: { fileName: 'a.wav' } });
    await waitFor(() => expect(container.textContent).toContain('uploader.cloudDefaultOption'));
    expect(container.textContent).toContain('settings.asrProvider.optionWithFlags');
    expect(container.textContent).not.toContain('uploader.highQuality');
  });
});
