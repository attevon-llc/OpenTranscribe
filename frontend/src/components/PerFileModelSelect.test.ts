/**
 * The model choice of an upload or reprocess names what each option returns (#1243): flags in
 * the native option text, the selected option's badges and what they mean under the select.
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

import PerFileModelSelect from './PerFileModelSelect.svelte';

const provider = (id: string, extra: Record<string, unknown> = {}) => ({
  provider: id,
  id,
  display_name: id === 'local' ? 'Local (GPU)' : 'Gladia',
  name: id,
  supports_diarization: true,
  speaker_count_hints: [],
  timestamp_granularity: 'word',
  status: 'tested',
  models: [],
  ...extra,
});

beforeEach(() => {
  vi.clearAllMocks();
  asrApi.getProviders.mockResolvedValue({
    providers: [provider('local'), provider('gladia', { supports_diarization: false })],
  });
});

const options = (c: HTMLElement) =>
  Array.from(c.querySelectorAll('option')).map((o) => o.textContent ?? '');
const noteKeys = (c: HTMLElement) =>
  Array.from(c.querySelectorAll('[data-testid="capability-note"]')).map((n) =>
    n.getAttribute('data-key')
  );

describe('PerFileModelSelect', () => {
  it('puts speakers and timing in every native option and explains the selected one', async () => {
    asrApi.getStatus.mockResolvedValue({ deployment_mode: 'full', is_cloud_provider: false });
    const { container } = render(PerFileModelSelect, { props: { adminModel: 'large-v3-turbo' } });
    await waitFor(() => expect(options(container)[0]).toContain('settings.asrProvider.flagsLine'));
    expect(options(container)).toHaveLength(2);
    for (const text of options(container)) {
      expect(text).toContain('settings.asrProvider.speakersIncluded');
      expect(text).toContain('settings.asrProvider.timing.word');
    }
    expect(noteKeys(container)).toEqual(['settings.asrProvider.implication.speakersLocal']);
  });

  it('names the saved cloud provider for "my setting" and warns when it is text only', async () => {
    asrApi.getStatus.mockResolvedValue({
      deployment_mode: 'full',
      is_cloud_provider: true,
      active_provider: 'gladia',
      active_model: 'solaria-1',
    });
    const { container } = render(PerFileModelSelect, { props: { adminModel: 'large-v3-turbo' } });
    await waitFor(() => expect(options(container)[0]).toContain('uploader.cloudDefaultOption'));
    expect(options(container)[0]).toContain('Gladia');
    expect(options(container)[0]).toContain('settings.asrProvider.textOnly');
    // The Fast model is this server's, so it still returns speakers (from this server).
    expect(options(container)[1]).toContain('settings.asrProvider.speakersIncluded');
    expect(noteKeys(container)).toEqual(['settings.asrProvider.implication.textOnly']);
  });

  it('offers only the provider on lite and drops a stored Fast choice', async () => {
    asrApi.getStatus.mockResolvedValue({
      deployment_mode: 'lite',
      is_cloud_provider: true,
      active_provider: 'gladia',
      active_model: 'solaria-1',
    });
    const { container } = render(PerFileModelSelect, { props: { value: 'base' } });
    await waitFor(() => expect(options(container)).toHaveLength(1));
    expect(noteKeys(container)).toEqual(['settings.asrProvider.implication.textOnlyLite']);
    expect((container.querySelector('select') as HTMLSelectElement).selectedIndex).toBe(0);
  });

  it('still offers both choices, without flags, when the catalog cannot be read', async () => {
    asrApi.getProviders.mockRejectedValue(new Error('down'));
    asrApi.getStatus.mockRejectedValue(new Error('down'));
    const { container } = render(PerFileModelSelect, {});
    await waitFor(() => expect(asrApi.getProviders).toHaveBeenCalled());
    expect(options(container)).toEqual([
      'uploader.highQualityOption{"model":"large-v3-turbo"}',
      'uploader.fastProcessingOption',
    ]);
    expect(container.querySelector('[data-testid="per-file-model-flags"]')).toBeNull();
  });
});
