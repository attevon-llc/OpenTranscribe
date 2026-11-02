import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockAxios = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

const mockStore = vi.hoisted(() => ({ loadTranscriptData: vi.fn() }));
vi.mock('$stores/transcriptStore', () => ({ transcriptStore: mockStore }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import { createSpeakerLoader, type SpeakerFile } from './speakerLoader';
import type { SpeakerItem, SpeakerNameBaseline } from './speakerNames';

function harness(file: SpeakerFile | null) {
  const state = {
    file,
    speakers: [] as SpeakerItem[],
    baseline: new Map() as SpeakerNameBaseline,
  };
  const loader = createSpeakerLoader({
    getFile: () => state.file,
    getSpeakerList: () => state.speakers,
    setSpeakerList: (s) => (state.speakers = s),
    getBaseline: () => state.baseline,
    setBaseline: (b) => (state.baseline = b),
  });
  return { state, loader };
}

const FILE: SpeakerFile = {
  uuid: 'f1',
  transcript_segments: [
    { uuid: 's1', start_time: 0, end_time: 1, text: 'hi', speaker_label: 'SPEAKER_01' },
  ] as unknown as SpeakerFile['transcript_segments'],
};

async function flush() {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

beforeEach(() => vi.clearAllMocks());

describe('createSpeakerLoader', () => {
  it('loads speakers, sets a trimmed baseline, and feeds the transcript store', async () => {
    mockAxios.get.mockResolvedValue({
      data: [{ uuid: 'a', name: 'SPEAKER_01', display_name: ' Ann ' }],
    });
    const { state, loader } = harness(FILE);

    await loader.loadSpeakers();

    expect(mockAxios.get).toHaveBeenCalledWith('/speakers', { params: { file_uuid: 'f1' } });
    expect(state.speakers[0]).toMatchObject({
      uuid: 'a',
      verified: false,
      showMatches: false,
      showSuggestions: false,
    });
    expect(state.baseline.get('a')).toBe('Ann');
    expect(mockStore.loadTranscriptData).toHaveBeenCalledWith(
      'f1',
      FILE.transcript_segments,
      state.speakers
    );
  });

  it('keeps an unsaved edit across a reload, and it still reads as unsaved', async () => {
    mockAxios.get.mockResolvedValue({ data: [{ uuid: 'a', name: 'S', display_name: 'Ann' }] });
    const { state, loader } = harness(FILE);
    await loader.loadSpeakers();
    state.speakers[0].display_name = 'Annie';

    await loader.loadSpeakers();

    expect(state.speakers[0].display_name).toBe('Annie');
    expect(state.baseline.get('a')).toBe('Ann');
  });

  it('drops a superseded response', async () => {
    let resolveFirst!: (v: unknown) => void;
    mockAxios.get
      .mockImplementationOnce(() => new Promise((r) => (resolveFirst = r)))
      .mockResolvedValueOnce({ data: [{ uuid: 'new', name: 'S' }] });
    const { state, loader } = harness(FILE);

    const first = loader.loadSpeakers();
    await loader.loadSpeakers();
    resolveFirst({ data: [{ uuid: 'old', name: 'S' }] });
    await first;

    expect(state.speakers.map((s) => s.uuid)).toEqual(['new']);
  });

  it('falls back to segment labels when the request fails', async () => {
    mockAxios.get.mockRejectedValue(new Error('down'));
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const { state, loader } = harness(FILE);

    await loader.loadSpeakers();

    expect(state.speakers).toEqual([{ name: 'SPEAKER_01', display_name: 'SPEAKER_01' }]);
  });

  it('attaches cross-media matches in the background for speakers that need them', async () => {
    mockAxios.get.mockImplementation((url: string) =>
      url === '/speakers'
        ? Promise.resolve({ data: [{ uuid: 'a', name: 'S', needsCrossMediaCall: true }] })
        : Promise.resolve({ data: [{ media_file_id: 'other' }] })
    );
    const { state, loader } = harness(FILE);

    await loader.loadSpeakers();
    await flush();

    expect(mockAxios.get).toHaveBeenCalledWith('/speakers/a/cross-media');
    expect(state.speakers[0].cross_video_matches).toEqual([{ media_file_id: 'other' }]);
  });

  it('skips cross-media for a file with diarization disabled', async () => {
    mockAxios.get.mockResolvedValue({
      data: [{ uuid: 'a', name: 'S', needsCrossMediaCall: true }],
    });
    const { state, loader } = harness({ ...FILE, diarization_disabled: true });

    await loader.loadSpeakers();
    await flush();

    expect(mockAxios.get).toHaveBeenCalledTimes(1);
    expect('cross_video_matches' in state.speakers[0]).toBe(false);
  });
});
