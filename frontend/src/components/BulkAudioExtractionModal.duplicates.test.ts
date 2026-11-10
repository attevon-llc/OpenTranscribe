import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import BulkAudioExtractionModal from './BulkAudioExtractionModal.svelte';

describe('BulkAudioExtractionModal with two identical videos', () => {
  it('lists both when name and modified time match', () => {
    const make = () => new File(['x'], 'clip.mp4', { type: 'video/mp4', lastModified: 1 });
    render(BulkAudioExtractionModal, { props: { isOpen: true, videoFiles: [make(), make()] } });
    expect(document.querySelectorAll('.file-item')).toHaveLength(2);
  });
});
