/**
 * The settings summary under the record button names the quality by its label, not the stored
 * code: "Quality: high" read as English in every locale.
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyWithOptionsLocale()
);

import MediaRecordPanel from './MediaRecordPanel.svelte';

describe('recording settings summary', () => {
  it('passes the translated quality label, not the raw setting code', () => {
    const { container } = render(MediaRecordPanel, {
      props: { recordingSupported: true, recordingQuality: 'high' },
    });

    expect(container.textContent).toContain('"quality":"settings.recording.qualityHigh"');
    expect(container.textContent).not.toContain('"quality":"high"');
  });
});
