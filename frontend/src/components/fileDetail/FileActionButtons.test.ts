/**
 * The header action row must render every action with ONE shared button style so the
 * Transcript / summary / Reprocess buttons are the same size in every state — the generate
 * button used to be a separate, smaller variant, most visible when no LLM is configured.
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import type { MediaFileDetail } from '$lib/types/media';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import FileActionButtons from './FileActionButtons.svelte';

const completedFile = {
  uuid: 'f1',
  filename: 'a.mp4',
  upload_time: '2026-01-01T00:00:00Z',
  status: 'completed',
  transcript_segments: [{ uuid: 's1' }],
  has_summary: false,
} as unknown as MediaFileDetail;

function buttons(container: HTMLElement) {
  return Array.from(container.querySelectorAll('.header-buttons button'));
}

describe('FileActionButtons — consistent sizing', () => {
  it('renders the unavailable generate button with the shared style, disabled and explained', () => {
    const { container } = render(FileActionButtons, {
      props: { file: completedFile, canEdit: true, llmAvailable: false },
    });

    const all = buttons(container);
    expect(all).toHaveLength(3);
    for (const b of all) expect(b.classList.contains('header-action-btn')).toBe(true);

    const generate = container.querySelector('.generate-summary-btn') as HTMLButtonElement;
    expect(generate.disabled).toBe(true);
    expect(generate.getAttribute('title')).toBe('fileDetail.aiNotAvailable');
  });

  it('keeps the shared style for the view and generating states', () => {
    const viewing = render(FileActionButtons, {
      props: { file: { ...completedFile, has_summary: true }, canEdit: true, llmAvailable: true },
    });
    for (const b of buttons(viewing.container))
      expect(b.classList.contains('header-action-btn')).toBe(true);
    viewing.unmount();

    const generating = render(FileActionButtons, {
      props: { file: completedFile, canEdit: true, llmAvailable: true, summaryGenerating: true },
    });
    for (const b of buttons(generating.container))
      expect(b.classList.contains('header-action-btn')).toBe(true);
  });
});
