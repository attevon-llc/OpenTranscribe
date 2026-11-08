/** Track labelling and the request field for a multi-track file (#1263). */
import { describe, it, expect } from 'vitest';
import en from '$lib/i18n/locales/en.json';
import { buildReprocessBody } from '$lib/fileDetail/reprocessStages';
import {
  audioTrackBodyField,
  describeTrack,
  languageName,
  selectableTracks,
  trackOptionLabel,
  transcribedTrack,
  type AudioStream,
} from '$lib/fileDetail/audioTracks';

const messages = en as Record<string, string>;
const t = (key: string, vars?: Record<string, unknown>) =>
  (messages[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_m, name) => String(vars?.[name] ?? ''));

const EN: AudioStream = { index: 0, codec: 'aac', channels: 1, language: 'eng', default: true };
const PT: AudioStream = {
  index: 1,
  codec: 'aac',
  channels: 2,
  language: 'por',
  title: 'Dublagem',
  default: false,
};

describe('selectableTracks', () => {
  it('offers a choice only when there are two or more tracks', () => {
    expect(selectableTracks([EN, PT])).toEqual([EN, PT]);
    expect(selectableTracks([EN])).toEqual([]);
    expect(selectableTracks(null)).toEqual([]);
    expect(selectableTracks(undefined)).toEqual([]);
  });
});

describe('transcribedTrack', () => {
  it('is the chosen track, else the first', () => {
    expect(transcribedTrack([EN, PT], 1)).toBe(PT);
    expect(transcribedTrack([EN, PT], null)).toBe(EN);
    expect(transcribedTrack(null, 1)).toBeNull();
  });
});

describe('languageName', () => {
  it('names ISO 639-2 and 639-1 codes in the UI language', () => {
    expect(languageName('por', 'en')).toBe('Portuguese');
    expect(languageName('pt', 'en')).toBe('Portuguese');
    expect(languageName('eng', 'de')).toBe('Englisch');
  });
  it('falls back to the code, and to nothing for no code', () => {
    expect(languageName('zzzz-not-a-code', 'en')).toBe('ZZZZ-NOT-A-CODE');
    expect(languageName(null, 'en')).toBeNull();
  });
});

describe('describeTrack / trackOptionLabel', () => {
  it('joins language, channels, codec and title', () => {
    expect(describeTrack(PT, t, 'en')).toBe('Portuguese · Stereo · AAC · Dublagem');
    expect(describeTrack(EN, t, 'en')).toBe('English · Mono · AAC');
  });
  it('says so when the container carries no language tag', () => {
    expect(describeTrack({ index: 0, codec: 'ac3', channels: 6 }, t, 'en')).toBe(
      'Language not tagged · 6ch · AC3'
    );
  });
  it('numbers tracks from 1 and marks the container default', () => {
    expect(trackOptionLabel(EN, t, 'en')).toBe('Track 1: English · Mono · AAC (default)');
    expect(trackOptionLabel(PT, t, 'en')).toBe('Track 2: Portuguese · Stereo · AAC · Dublagem');
  });
});

describe('audioTrackBodyField', () => {
  it('sends a chosen track the file has, and nothing otherwise', () => {
    expect(audioTrackBodyField(1, [EN, PT])).toEqual({ audio_stream_index: 1 });
    expect(audioTrackBodyField(0, [EN, PT])).toEqual({ audio_stream_index: 0 });
    expect(audioTrackBodyField(null, [EN, PT])).toEqual({});
    expect(audioTrackBodyField(1, [EN])).toEqual({});
    expect(audioTrackBodyField(5, [EN, PT])).toEqual({});
  });
});

describe('buildReprocessBody with a track', () => {
  const base = {
    showSpeakerSettings: false,
    settings: null,
    systemDefaults: null,
    minSpeakers: null,
    maxSpeakers: null,
    numSpeakers: null,
    model: null,
    modelChoiceEnabled: false,
    audioStreams: [EN, PT],
    audioStreamIndex: 1,
  };
  it('carries the track on a stage that decodes audio', () => {
    expect(buildReprocessBody({ ...base, stages: ['transcription'] })).toEqual({
      stages: ['transcription'],
      audio_stream_index: 1,
    });
    expect(buildReprocessBody({ ...base, stages: ['rediarize'] })).toMatchObject({
      audio_stream_index: 1,
    });
  });
  it('omits it on stages the server would refuse it for (422)', () => {
    expect(buildReprocessBody({ ...base, stages: ['summarization'] })).toEqual({
      stages: ['summarization'],
    });
  });
});
