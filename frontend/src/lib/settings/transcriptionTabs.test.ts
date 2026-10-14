import { describe, it, expect } from 'vitest';
import {
  resolveTranscriptionTab,
  transcriptionTabs,
  transcriptionVisible,
  type TranscriptionAccess,
} from './transcriptionTabs';

const access = (over: Partial<TranscriptionAccess> = {}): TranscriptionAccess => ({
  prefsCap: true,
  asrCap: true,
  vocabCap: true,
  ...over,
});

const ids = (a: TranscriptionAccess) => transcriptionTabs(a).map((tab) => tab.id);

describe('transcriptionTabs', () => {
  it('offers all four tabs, unlocked, in the documented order', () => {
    expect(transcriptionTabs(access())).toEqual([
      { id: 'tx-language', locked: false },
      { id: 'tx-provider', locked: false },
      { id: 'tx-vocabulary', locked: false },
      { id: 'tx-accuracy', locked: false },
    ]);
  });

  it('drops the provider tab without the ASR capability', () => {
    expect(ids(access({ asrCap: false }))).toEqual(['tx-language', 'tx-vocabulary', 'tx-accuracy']);
  });

  it('drops language and accuracy without the prefs capability', () => {
    expect(ids(access({ prefsCap: false }))).toEqual(['tx-provider', 'tx-vocabulary']);
  });

  it('drops the vocabulary tab without the vocabulary capability', () => {
    expect(ids(access({ vocabCap: false }))).toEqual(['tx-language', 'tx-provider', 'tx-accuracy']);
  });

  it('hides the whole section when every capability is off', () => {
    const none = access({ prefsCap: false, asrCap: false, vocabCap: false });
    expect(transcriptionVisible(none)).toBe(false);
    expect(transcriptionVisible(access())).toBe(true);
  });
});

describe('resolveTranscriptionTab', () => {
  const tabs = transcriptionTabs(access({ prefsCap: false }));

  it('honours a requested tab that is offered', () => {
    expect(resolveTranscriptionTab('tx-vocabulary', tabs)).toBe('tx-vocabulary');
  });

  it('falls back to the first tab when the requested one is absent', () => {
    expect(resolveTranscriptionTab('tx-language', tabs)).toBe('tx-provider');
  });

  it('returns null with no tabs', () => {
    expect(resolveTranscriptionTab('tx-language', [])).toBeNull();
  });
});
