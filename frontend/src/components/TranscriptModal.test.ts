import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import { writable } from 'svelte/store';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$lib/i18n', () => ({ translateSpeakerLabel: (name: string) => name }));

// The factory is hoisted above every top-level binding, so the store has to be created
// INSIDE it and read back through the mocked module afterwards.
vi.mock('../stores/transcriptStore', async () => {
  const { writable: mkStore } = await import('svelte/store');
  return {
    processedTranscriptSegments: mkStore<unknown[]>([]),
    transcriptStore: mkStore(null),
  };
});

import TranscriptModal from './TranscriptModal.svelte';
import { processedTranscriptSegments } from '../stores/transcriptStore';

const segments = processedTranscriptSegments as unknown as ReturnType<typeof writable<unknown[]>>;

const SEGMENTS = [
  {
    text: 'the number he gave was on the invoice',
    speakerName: 'SPEAKER_00',
    startTime: 0,
    endTime: 4,
    rawStartIndex: 0,
  },
];

beforeEach(() => {
  segments.set(SEGMENTS);
});

function renderModal(props: Record<string, unknown> = {}, events: Record<string, unknown> = {}) {
  // Svelte 5 removed `component.$on(...)`; dispatched events are observed through
  // testing-library's `events` option (same as SegmentSpeakerDropdown/CopyButton here).
  return render(TranscriptModal, {
    props: { isOpen: true, fileId: 1, fileName: 'meeting.mp4', ...props },
    events,
  } as never);
}

describe('TranscriptModal copy (issue #821)', () => {
  it('asks the parent to copy instead of copying anything itself', async () => {
    const copied = vi.fn();
    renderModal({}, { copyTranscript: copied });

    await fireEvent.click(screen.getByRole('button', { name: 'transcriptModal.copyTranscript' }));

    // The parent's handler is what calls GET /files/{uuid}/export — the server is what
    // resolves export_locked. A dispatch here is the whole fix: this component cannot
    // ask anything about policy, so it must not decide what leaves the app.
    expect(copied).toHaveBeenCalledTimes(1);
  });

  it('reflects the parent-owned copy status rather than tracking its own', async () => {
    const { rerender } = renderModal({ copyStatus: 'idle' });
    expect(
      screen.getByRole('button', { name: 'transcriptModal.copyTranscript' })
    ).toHaveTextContent('transcriptModal.copy');

    await rerender({ isOpen: true, fileId: 1, fileName: 'meeting.mp4', copyStatus: 'copied' });
    expect(
      screen.getByRole('button', { name: 'transcriptModal.copyTranscript' })
    ).toHaveTextContent('transcriptModal.copied');
  });

  it('disables the button while a copy is in flight, so a refusal cannot be raced', async () => {
    renderModal({ copyStatus: 'copying' });

    expect(screen.getByRole('button', { name: 'transcriptModal.copyTranscript' })).toBeDisabled();
  });

  it('offers no copy button at all when there is nothing rendered to copy', () => {
    segments.set([]);
    renderModal();

    expect(
      screen.queryByRole('button', { name: 'transcriptModal.copyTranscript' })
    ).not.toBeInTheDocument();
  });
});
