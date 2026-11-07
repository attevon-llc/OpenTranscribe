import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
}));
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));

const api = vi.hoisted(() => ({
  getAudioExtractionSettings: vi.fn(),
  updateAudioExtractionSettings: vi.fn(),
}));
vi.mock('$lib/api/audioExtractionSettings', () => api);

import AudioExtractionSettings from './AudioExtractionSettings.svelte';

beforeEach(() => {
  api.getAudioExtractionSettings.mockResolvedValue({
    auto_extract_enabled: true,
    extraction_threshold_mb: 100,
    remember_choice: false,
    show_modal: true,
  });
});

describe('AudioExtractionSettings layout', () => {
  it('puts the file-size field directly under the switch it depends on, before the other choices', async () => {
    const { container } = render(AudioExtractionSettings);
    await screen.findByTestId('threshold-field');

    const order = Array.from(container.querySelectorAll('input')).map((i) => i.id);
    expect(order).toEqual(['auto-extract', 'threshold', 'show-modal', 'remember-choice']);

    const enableCard = container.querySelector('.toggle-group');
    expect(enableCard?.querySelector('#threshold')).not.toBeNull();
    expect(enableCard?.querySelector('#show-modal')).toBeNull();
  });

  it('labels the field above its control', async () => {
    const { container } = render(AudioExtractionSettings);
    const field = await screen.findByTestId('threshold-field');
    const children = Array.from(field.children).map((c) => c.tagName + '.' + c.className);
    expect(children[0]).toMatch(/^LABEL/);
    expect(container.querySelector('label[for="threshold"]')).not.toBeNull();
  });

  it('hides the dependent field when extraction is switched off', async () => {
    render(AudioExtractionSettings);
    await screen.findByTestId('threshold-field');
    await fireEvent.click(document.getElementById('auto-extract') as HTMLElement);
    expect(screen.queryByTestId('threshold-field')).toBeNull();
  });
});
