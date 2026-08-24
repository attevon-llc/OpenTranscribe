/**
 * G2: the microphone stayed open after "Stop Recording". `stopRecording()`
 * stopped the `MediaRecorder` and cleared the duration interval but never
 * released the hardware (`cleanupRecording()` — mic tracks, `AudioContext`,
 * level-meter rAF) — that was reachable only from `clearRecording()` or the
 * start-error path, so record -> stop -> navigate away left the browser's
 * mic indicator lit for the rest of the SPA session.
 *
 * `cleanupRecording()` had to move into `MediaRecorder.onstop` rather than
 * straight into `stopRecording()`: `onstop` fires asynchronously and is what
 * builds `recordedBlob` from `recordedChunks`, so clearing that array
 * synchronously in `stopRecording()` would race the event and could empty
 * the blob the user is about to upload. These tests exercise the real
 * `RecordingManager` against fake MediaRecorder/getUserMedia/AudioContext
 * implementations rather than reimplementing the race in isolation.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

class FakeTrack {
  readyState: 'live' | 'ended' = 'live';
  stop = vi.fn(() => {
    this.readyState = 'ended';
  });
}

function makeStream(tracks: FakeTrack[]) {
  return { getTracks: () => tracks } as unknown as MediaStream;
}

class FakeMediaRecorder {
  state: 'inactive' | 'recording' | 'paused' = 'inactive';
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  constructor(public stream: MediaStream) {}
  start() {
    this.state = 'recording';
  }
  stop() {
    this.state = 'inactive';
    // Real MediaRecorder fires 'stop' asynchronously, after `.stop()` returns —
    // that async gap is the exact race this fix has to survive.
    setTimeout(() => this.onstop?.(), 0);
  }
  pause() {
    this.state = 'paused';
  }
  resume() {
    this.state = 'recording';
  }
}

class FakeAudioContext {
  state: 'running' | 'suspended' | 'closed' = 'running';
  close = vi.fn(async () => {
    this.state = 'closed';
  });
  resume = vi.fn(async () => {
    this.state = 'running';
  });
  createMediaStreamSource() {
    return { connect: vi.fn() };
  }
  createAnalyser() {
    return { fftSize: 0, smoothingTimeConstant: 0, connect: vi.fn() };
  }
}

let tracks: FakeTrack[];

beforeEach(() => {
  vi.resetModules();
  tracks = [new FakeTrack(), new FakeTrack()];

  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: {
      getUserMedia: vi.fn(async () => makeStream(tracks)),
      enumerateDevices: vi.fn(async () => []),
    },
  });
  vi.stubGlobal('MediaRecorder', FakeMediaRecorder);
  vi.stubGlobal('AudioContext', FakeAudioContext);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function startedManager() {
  const { recordingManager, recordingStore } = await import('./recording');
  recordingStore.update((s) => ({ ...s, recordingSupported: true }));
  await recordingManager.startRecording();
  // Let the 100ms `updateAudioLevel` setTimeout and any pending microtasks settle
  // before the test drives stop/close, matching real user timing.
  await new Promise((r) => setTimeout(r, 0));
  return { recordingManager, recordingStore };
}

describe('RecordingManager — hardware release on stop (G2)', () => {
  it('every track is stopped after stopRecording(), without clicking Upload/Delete', async () => {
    const { recordingManager } = await startedManager();

    recordingManager.stopRecording();
    // cleanup runs inside the async 'stop' event — wait for it.
    await new Promise((r) => setTimeout(r, 0));

    expect(tracks.every((t) => t.readyState === 'ended')).toBe(true);
    expect(tracks.every((t) => t.stop.mock.calls.length === 1)).toBe(true);
  });

  it('control: the recorded blob is still available for upload after stopRecording()', async () => {
    const { recordingManager, recordingStore } = await startedManager();

    recordingManager.stopRecording();
    await new Promise((r) => setTimeout(r, 0));

    const state = get(recordingStore);
    expect(state.recordedBlob).toBeInstanceOf(Blob);
  });
});
