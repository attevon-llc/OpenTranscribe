/**
 * `PIPELINE_ISSUE_KEYS` is the only place an issue code gets its sentence. The privacy work
 * (issue #1280) added `voiceprints_disabled`: the resolver emits it when a recording's owner,
 * organization or the operator has voiceprints off. Without its entry the code would render as
 * a raw key (or nothing) in the Processing pipeline view.
 */
import { describe, it, expect } from 'vitest';
import en from '$lib/i18n/locales/en.json';
import { PIPELINE_ISSUE_KEYS } from './pipelineKeys';
import { PIPELINE_ISSUE_CODES, issueKey, fixTargets } from './pipelineIssues';

const EN = en as Record<string, string>;

describe('PIPELINE_ISSUE_KEYS', () => {
  it('has a sentence for voiceprints_disabled, as an explicit literal key', () => {
    expect(PIPELINE_ISSUE_KEYS.voiceprints_disabled).toBe(
      'settings.pipeline.issue.voiceprints_disabled'
    );
    expect(issueKey('voiceprints_disabled')).toBe('settings.pipeline.issue.voiceprints_disabled');
  });

  it('resolves voiceprints_disabled to real English copy about speaker recognition', () => {
    const sentence = EN[PIPELINE_ISSUE_KEYS.voiceprints_disabled];
    expect(sentence).toBeTruthy();
    expect(sentence).toMatch(/Speaker recognition/);
  });

  it('is exactly the set of issue codes the UI recognises (none missing, none stray)', () => {
    expect(Object.keys(PIPELINE_ISSUE_KEYS).sort()).toEqual([...PIPELINE_ISSUE_CODES].sort());
  });

  it('gives every code a distinct key that exists in en.json', () => {
    const keys = PIPELINE_ISSUE_CODES.map((code) => issueKey(code));
    expect(new Set(keys).size).toBe(PIPELINE_ISSUE_CODES.length);
    expect(keys.filter((key) => !(key in EN))).toEqual([]);
  });

  it('offers no fix link for voiceprints_disabled (no pipeline setting turns it back on)', () => {
    // An unknown code would fall back to its step's section (speaker identification), which
    // cannot turn an owner's or organization's switch back on.
    expect(fixTargets({ code: 'voiceprints_disabled', step: 'speaker_id' })).toEqual([]);
  });
});
