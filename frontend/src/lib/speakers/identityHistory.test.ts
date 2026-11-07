import { describe, expect, it } from 'vitest';
import type { SpeakerIdentityEvent } from '$lib/types/speaker';
import { actorOf, changeOf, confidencePercent, kindKey } from './identityHistory';

function entry(overrides: Partial<SpeakerIdentityEvent>): SpeakerIdentityEvent {
  return {
    uuid: 'e-1',
    kind: 'rename',
    at: '2026-11-07T10:00:00Z',
    actor: { kind: 'user', user: { uuid: 'u-1', name: 'Dana' } },
    ...overrides,
  };
}

describe('changeOf', () => {
  it('shows a rename as old name to new name', () => {
    const change = changeOf(entry({ before: { name: 'Bob' }, after: { name: 'Robert' } }));
    expect(change).toEqual({ from: { text: 'Bob' }, to: { text: 'Robert' } });
  });

  it('never shows a name a voiceprint wrote in a person entry, only that it was withheld', () => {
    const change = changeOf(
      entry({
        before: { withheld: ['name'], identity_source: 'voiceprint' },
        after: { name: 'Al' },
      })
    );
    expect(change?.from).toEqual({ key: 'speakerHistory.withheld' });
    expect(change?.to).toEqual({ text: 'Al' });
  });

  it('shows a link by profile name, and a deleted profile as such', () => {
    expect(
      changeOf(
        entry({
          kind: 'link',
          before: { profile: null },
          after: { profile: { uuid: 'p', name: 'Ann' } },
        })
      )
    ).toEqual({ from: { key: 'speakerHistory.none' }, to: { text: 'Ann' } });
    expect(
      changeOf(
        entry({
          kind: 'unlink',
          before: { profile: { uuid: null, name: null, deleted: true } },
          after: { profile: null },
        })
      )
    ).toEqual({
      from: { key: 'speakerHistory.deletedProfile' },
      to: { key: 'speakerHistory.none' },
    });
  });

  it('names the absorbed speaker of a merge', () => {
    expect(changeOf(entry({ kind: 'merge', before: { merged_name: 'SPEAKER_03' } }))).toEqual({
      from: { text: 'SPEAKER_03' },
      to: null,
    });
  });

  it('returns null for a change that touches no shown value', () => {
    expect(changeOf(entry({ kind: 'purge', before: null, after: null }))).toBeNull();
    expect(changeOf(entry({ kind: 'confirmed', before: { identity_source: null } }))).toBeNull();
  });
});

describe('actorOf', () => {
  it('names a person, hides one the viewer may not see, and marks an erased account', () => {
    expect(actorOf(entry({}))).toEqual({ text: 'Dana' });
    expect(actorOf(entry({ actor: { kind: 'user', user: null } }))).toEqual({
      key: 'speakerHistory.byPerson',
    });
    expect(actorOf(entry({ actor: { kind: 'user', user: { uuid: null, erased: true } } }))).toEqual(
      {
        key: 'speakerHistory.byErased',
      }
    );
    expect(actorOf(entry({ actor: { kind: 'system', user: null } }))).toEqual({
      key: 'speakerHistory.bySystem',
    });
  });
});

describe('kindKey and confidencePercent', () => {
  it('maps a kind to its key and a confidence to a whole percentage', () => {
    expect(kindKey(entry({ kind: 'suggestion_rejected' }))).toBe(
      'speakerHistory.kind.suggestion_rejected'
    );
    expect(confidencePercent(entry({ confidence: 0.913 }))).toBe(91);
    expect(confidencePercent(entry({ confidence: null }))).toBeNull();
  });
});
