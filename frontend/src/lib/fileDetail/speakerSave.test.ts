import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockAxios = vi.hoisted(() => ({ put: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

const mockStore = vi.hoisted(() => ({ updateSpeakerName: vi.fn(), loadTranscriptData: vi.fn() }));
vi.mock('$stores/transcriptStore', () => ({ transcriptStore: mockStore }));

const mockToast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, o?: Record<string, unknown>) => string) => void) => {
      run((key: string, o?: Record<string, unknown>) => (o ? `${key}:${JSON.stringify(o)}` : key));
      return () => {};
    },
  },
}));

import { createSpeakerSaveFlow } from './speakerSave';
import { baselineFrom, type SpeakerItem } from './speakerNames';
import type { SegmentBearingFile } from './segmentSync';
import type { SpeakerFile } from './speakerLoader';

function harness(speakers: SpeakerItem[], opts: { diarizationDisabled?: boolean } = {}) {
  const state = {
    file: {
      uuid: 'f1',
      transcript_segments: speakers.map((s, i) => ({
        uuid: `seg-${i}`,
        speaker_id: s.uuid,
        speaker_label: s.name,
        resolved_speaker_name: s.display_name,
      })),
      grouped_segments: [],
    } as SegmentBearingFile,
    speakers,
    baseline: baselineFrom(speakers),
    saving: false,
    dialog: null as { title: string; message: string } | null,
    editorOpen: true,
  };
  const deps = {
    loadSpeakers: vi.fn(async () => {}),
    refreshCaptionTrack: vi.fn(),
    refreshAnalytics: vi.fn(),
  };
  const flow = createSpeakerSaveFlow({
    getFile: () => state.file as unknown as SpeakerFile,
    setFile: (f) => (state.file = f),
    getSpeakerList: () => state.speakers,
    setSpeakerList: (s) => (state.speakers = s),
    getBaseline: () => state.baseline,
    setBaseline: (b) => (state.baseline = b),
    setSavingSpeakers: (v) => (state.saving = v),
    setProfileConfirmation: (d) => (state.dialog = d),
    closeSpeakerEditor: () => (state.editorOpen = false),
    isDiarizationDisabled: () => opts.diarizationDisabled ?? false,
    ...deps,
  });
  return { state, flow, deps };
}

const ann = (o: Partial<SpeakerItem> = {}): SpeakerItem => ({
  uuid: 'a',
  name: 'SPEAKER_00',
  display_name: 'Ann',
  verified: false,
  ...o,
});
const bob = (o: Partial<SpeakerItem> = {}): SpeakerItem => ({
  uuid: 'b',
  name: 'SPEAKER_01',
  display_name: 'Bob',
  verified: false,
  ...o,
});

function segmentName(state: ReturnType<typeof harness>['state'], i: number) {
  return state.file.transcript_segments?.[i]?.resolved_speaker_name;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.put.mockResolvedValue({ data: {} });
  mockAxios.delete.mockResolvedValue({});
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('single rename', () => {
  it('renames optimistically, saves, and moves the baseline', async () => {
    const { state, flow, deps } = harness([ann()]);

    await flow.requestSpeakerRename('a', 'Annie');

    expect(mockAxios.put).toHaveBeenCalledWith('/speakers/a', { display_name: 'Annie' });
    expect(state.speakers[0].display_name).toBe('Annie');
    expect(segmentName(state, 0)).toBe('Annie');
    expect(state.baseline.get('a')).toBe('Annie');
    expect(mockToast.success).toHaveBeenCalledTimes(1);
    expect(deps.refreshCaptionTrack).toHaveBeenCalledWith('speaker rename');
    expect(deps.refreshAnalytics).toHaveBeenCalled();
    expect(deps.loadSpeakers).toHaveBeenCalled();
  });

  it('undoes the optimistic write when the server refuses', async () => {
    mockAxios.put.mockRejectedValue({ response: { status: 403 } });
    const { state, flow } = harness([ann()]);

    await flow.requestSpeakerRename('a', 'Annie');

    expect(state.speakers[0].display_name).toBe('Ann');
    expect(segmentName(state, 0)).toBe('Ann');
    expect(state.baseline.get('a')).toBe('Ann');
    expect(mockToast.error.mock.calls[0][0]).toContain('speakerProfile.permissionDenied');
  });

  it('refuses an invalid name without a request', async () => {
    const { state, flow } = harness([ann()]);
    await flow.requestSpeakerRename('a', '   ');
    expect(state.speakers[0].display_name).toBe('Ann');
    expect(mockAxios.put).not.toHaveBeenCalled();
    expect(mockToast.error).toHaveBeenCalledWith('speakerValidation.nameEmpty');
  });

  it('asks about a linked profile first, and sends the decision', async () => {
    const { state, flow } = harness([ann({ profile: { uuid: 'p', name: 'Ann' } })]);

    await flow.requestSpeakerRename('a', 'Annie');
    expect(state.dialog?.title).toBe('speakerProfile.updateTitle');
    expect(mockAxios.put).not.toHaveBeenCalled();

    const done = flow.decideProfile('update_profile');
    // Closed before the request settles.
    expect(state.dialog).toBeNull();
    await done;
    expect(mockAxios.put).toHaveBeenCalledWith('/speakers/a', {
      display_name: 'Annie',
      profile_action: 'update_profile',
    });
  });

  it('a cancelled profile decision saves nothing', async () => {
    const { state, flow } = harness([ann({ profile: { uuid: 'p', name: 'Ann' } })]);
    await flow.requestSpeakerRename('a', 'Annie');

    flow.cancelProfileDecision();
    await flow.decideProfile('update_profile');

    expect(state.dialog).toBeNull();
    expect(mockAxios.put).not.toHaveBeenCalled();
  });
});

describe('bulk save', () => {
  it('saves only changed speakers, closes the editor, and paints a clear with the raw label', async () => {
    const { state, flow } = harness([ann(), bob()]);
    state.speakers[0].display_name = '';

    await flow.saveAllSpeakerNames();

    expect(mockAxios.put).toHaveBeenCalledTimes(1);
    expect(mockAxios.put).toHaveBeenCalledWith('/speakers/a', {
      display_name: '',
      name: 'SPEAKER_00',
    });
    expect(segmentName(state, 0)).toBe('SPEAKER_00');
    expect(state.saving).toBe(false);
    expect(state.editorOpen).toBe(false);
    expect(state.baseline.get('a')).toBe('');
    expect(mockToast.success).toHaveBeenCalledWith('speakerProfile.savedSuccess');
    expect(mockAxios.delete).toHaveBeenCalledWith('/files/f1/cache');
  });

  it('restores the names that failed and reports a partial failure', async () => {
    mockAxios.put.mockImplementation((url: string) =>
      url === '/speakers/b' ? Promise.reject(new Error('x')) : Promise.resolve({ data: {} })
    );
    const { state, flow } = harness([ann(), bob()]);
    state.speakers[0].display_name = 'Annie';
    state.speakers[1].display_name = 'Robert';

    await flow.saveAllSpeakerNames();

    expect(state.speakers.map((s) => s.display_name)).toEqual(['Annie', 'Bob']);
    expect(segmentName(state, 0)).toBe('Annie');
    expect(segmentName(state, 1)).toBe('Bob');
    expect(mockToast.error.mock.calls[0][0]).toContain('speakerProfile.bulkSavePartialFailure');
    expect(mockToast.success).not.toHaveBeenCalled();
  });

  it('does nothing, and announces nothing, when no name changed', async () => {
    const { state, flow } = harness([ann()]);
    await flow.saveAllSpeakerNames();
    expect(mockAxios.put).not.toHaveBeenCalled();
    expect(mockToast.success).not.toHaveBeenCalled();
    expect(state.saving).toBe(false);
    expect(state.editorOpen).toBe(true);
  });

  it('walks the profile-decision queue, then saves with each decision', async () => {
    const { state, flow } = harness([
      ann({ profile: { uuid: 'pa', name: 'Ann' } }),
      bob({ profile: { uuid: 'pb', name: 'Bob' } }),
    ]);
    state.speakers[0].display_name = 'Annie';
    state.speakers[1].display_name = 'Robert';

    await flow.saveAllSpeakerNames();
    expect(state.dialog?.title).toContain('"current":1');
    await flow.decideProfile('update_profile');
    expect(state.dialog?.title).toContain('"current":2');
    expect(mockAxios.put).not.toHaveBeenCalled();
    await flow.decideProfile('create_new_profile');

    expect(state.dialog).toBeNull();
    expect(mockAxios.put).toHaveBeenCalledWith('/speakers/a', {
      display_name: 'Annie',
      name: 'SPEAKER_00',
      profile_action: 'update_profile',
    });
    expect(mockAxios.put).toHaveBeenCalledWith('/speakers/b', {
      display_name: 'Robert',
      name: 'SPEAKER_01',
      profile_action: 'create_new_profile',
    });
  });

  it('cancelling the queue abandons the bulk save and stops the spinner', async () => {
    const { state, flow } = harness([ann({ profile: { uuid: 'pa', name: 'Ann' } })]);
    state.speakers[0].display_name = 'Annie';

    await flow.saveAllSpeakerNames();
    expect(state.saving).toBe(true);
    flow.cancelProfileDecision();

    expect(state.saving).toBe(false);
    expect(state.dialog).toBeNull();
    expect(mockAxios.put).not.toHaveBeenCalled();
  });
});
