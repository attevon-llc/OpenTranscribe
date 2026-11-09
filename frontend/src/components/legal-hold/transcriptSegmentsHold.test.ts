/**
 * Segment Edit on a held file stays visible, disabled, pointing at the note above the
 * transcript (legal-hold plan, Lane D). A read-only support session, which is not a hold,
 * still shows no Edit at all.
 */
import { describe, it, expect, vi, beforeAll } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const mocks = await import('$testMocks/galleryToolbarMocks');
  const { readable } = await import('svelte/store');
  return { ...mocks.englishLocale(), locale: readable('en') };
});

import FrozenContextTestHost from './FrozenContextTestHost.svelte';
import TranscriptSegmentList from '$components/transcript/TranscriptSegmentList.svelte';

beforeAll(() => {
  class NoopObserver {
    observe() {}
    disconnect() {}
    unobserve() {}
    takeRecords() {
      return [];
    }
  }
  vi.stubGlobal('IntersectionObserver', NoopObserver);
});

const seg = {
  uuid: 's1',
  start_time: 0,
  end_time: 5,
  text: 'Hello there',
  speaker_label: 'SPEAKER_00',
};
const props = {
  file: { uuid: 'f1', transcript_segments: [seg] },
  groupedTranscriptSegments: [
    { isOverlapGroup: false, startSegmentIndex: 0, startTime: 0, endTime: 5, segments: [seg] },
  ],
  transcriptSegments: [seg],
  diarizationDisabled: true,
  totalSegments: 1,
};

function list(editable: boolean, frozen: boolean) {
  return render(FrozenContextTestHost, {
    props: { component: TranscriptSegmentList, props: { ...props, editable }, frozen },
  } as never);
}

describe('transcript segments on a held file', () => {
  it('show Edit disabled and described by the transcript note', () => {
    list(false, true);
    const edit = screen.getByRole('button', { name: 'Edit' });
    expect(edit).toBeDisabled();
    expect(edit).toHaveAttribute('aria-describedby', 'transcript-frozen-note');
  });

  it('show an enabled Edit when the file is free and editable', () => {
    list(true, false);
    expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled();
  });

  it('show no Edit for a read-only viewer of a free file', () => {
    list(false, false);
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
  });
});
