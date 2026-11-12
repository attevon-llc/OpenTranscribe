/**
 * The transcript, comment and bulk-select write controls on a public read-only demo (#628,
 * P-B): visible but disabled, each saying why. Every case has a not-a-demo control so a
 * "disabled" assertion cannot pass on a control that was never enabled in the first place.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyLocale({ code: 'en' })
);
vi.mock('$stores/supportSession', async () => {
  const { writable } = await import('svelte/store');
  return { supportSessionGate: writable({ active: false, readOnly: false }) };
});
vi.mock('$stores/gallery', async () => {
  const { writable, readable } = await import('svelte/store');
  return {
    galleryStore: new Proxy({}, { get: () => () => {} }),
    galleryState: writable({ isSelecting: false, selectedFiles: new Set<string>() }),
    selectedCount: writable(0),
    allFilesSelected: readable(false),
  };
});

import TranscriptActionsBar from '$components/transcript/TranscriptActionsBar.svelte';
import TranscriptSegmentList from '$components/transcript/TranscriptSegmentList.svelte';
import CommentComposer from '$components/comments/CommentComposer.svelte';
import GallerySelectionActions from '$components/gallery/GallerySelectionActions.svelte';
import { demoMode } from '$stores/demo';

const TOOLTIP = 'demo.readOnlyTooltip';
const segment = {
  uuid: 'a',
  start_time: 0,
  end_time: 5,
  text: 'Segment 0',
  speaker_label: 'SPEAKER_00',
};
const segmentProps = {
  file: { uuid: 'f1', transcript_segments: [segment] },
  groupedTranscriptSegments: [
    { isOverlapGroup: false, startSegmentIndex: 0, startTime: 0, endTime: 5, segments: [segment] },
  ],
  transcriptSegments: [segment],
  diarizationDisabled: true,
  totalSegments: 1,
  editable: true,
};

class NoopObserver {
  observe() {}
  disconnect() {}
  unobserve() {}
  takeRecords() {
    return [];
  }
}

beforeEach(() => {
  demoMode.set(false);
  vi.stubGlobal('IntersectionObserver', NoopObserver);
});
afterEach(() => vi.unstubAllGlobals());

describe('Edit Speakers', () => {
  const props = {
    file: { uuid: 'f1', download_url: 'https://x/y' } as never,
    diarizationDisabled: false,
  };

  it('is enabled outside a demo', () => {
    const { container } = render(TranscriptActionsBar, { props });
    expect((container.querySelector('.edit-speakers-button') as HTMLButtonElement).disabled).toBe(
      false
    );
  });

  it('is disabled in a demo, with the explanation as its tooltip', () => {
    demoMode.set(true);
    const { container } = render(TranscriptActionsBar, { props });
    const button = container.querySelector('.edit-speakers-button') as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('title')).toBe(TOOLTIP);
  });
});

describe('segment Edit', () => {
  it('is enabled outside a demo', () => {
    const { container } = render(TranscriptSegmentList, { props: segmentProps });
    expect((container.querySelector('.edit-button') as HTMLButtonElement).disabled).toBe(false);
  });

  it('is disabled in a demo even though the viewer is otherwise allowed to edit', () => {
    demoMode.set(true);
    const { container } = render(TranscriptSegmentList, { props: segmentProps });
    const buttons = container.querySelectorAll<HTMLButtonElement>('.edit-button');
    expect(buttons).toHaveLength(1);
    expect(buttons[0].disabled).toBe(true);
    expect(buttons[0].getAttribute('title')).toBe(TOOLTIP);
  });
});

describe('Add comment', () => {
  const props = { submit: async () => true };

  it('is enabled outside a demo', () => {
    const { container } = render(CommentComposer, { props });
    expect((container.querySelector('.submit-button') as HTMLButtonElement).disabled).toBe(false);
  });

  it('is disabled in a demo, with the explanation as its tooltip', () => {
    demoMode.set(true);
    const { container } = render(CommentComposer, { props });
    const button = container.querySelector('.submit-button') as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('title')).toBe(TOOLTIP);
  });
});

describe('Select files (bulk operations)', () => {
  const props = { files: [] as never };

  it('is enabled outside a demo', () => {
    const { container } = render(GallerySelectionActions, { props });
    expect((container.querySelector('.select-btn') as HTMLButtonElement).disabled).toBe(false);
  });

  it('is disabled in a demo, with the explanation as its tooltip', () => {
    demoMode.set(true);
    const { container } = render(GallerySelectionActions, { props });
    const button = container.querySelector('.select-btn') as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('title')).toBe(TOOLTIP);
  });
});
