import { describe, it, expect } from 'vitest';
import {
  compareBuilds,
  normalizeCommit,
  parseBuildVersion,
  sourceRefFor,
  type BuildComparison,
} from './buildIdentity';

const FULL_SHA = 'abc1234' + 'f'.repeat(33);

describe('parseBuildVersion', () => {
  it.each([
    ['v0.6.0', { kind: 'release', semver: '0.6.0' }],
    ['0.6.0', { kind: 'release', semver: '0.6.0' }],
    ['V1.22.333', { kind: 'release', semver: '1.22.333' }],
    ['  v0.6.0\n', { kind: 'release', semver: '0.6.0' }],
    ['dev-abc1234', { kind: 'dev', commit: 'abc1234' }],
    ['DEV-ABC1234', { kind: 'dev', commit: 'abc1234' }],
    ['unknown', { kind: 'unknown' }],
    ['', { kind: 'unknown' }],
    [null, { kind: 'unknown' }],
    [undefined, { kind: 'unknown' }],
    ['dev-xyz', { kind: 'unknown' }],
    ['dev-abc12', { kind: 'unknown' }],
    ['0.6', { kind: 'unknown' }],
    ['0.6.0-rc1', { kind: 'unknown' }],
  ])('%j -> %j', (input, expected) => {
    expect(parseBuildVersion(input)).toEqual(expected);
  });
});

describe('normalizeCommit', () => {
  it.each([
    ['abc1234', { sha: 'abc1234', dirty: false }],
    ['ABC1234', { sha: 'abc1234', dirty: false }],
    ['abc1234-dirty', { sha: 'abc1234', dirty: true }],
    [FULL_SHA, { sha: FULL_SHA, dirty: false }],
    ['unknown', null],
    ['', null],
    [null, null],
    [undefined, null],
    ['abc12', null],
    ['not-a-sha', null],
    ['a'.repeat(41), null],
  ])('%j -> %j', (input, expected) => {
    expect(normalizeCommit(input)).toEqual(expected);
  });
});

describe('compareBuilds', () => {
  const cases: [string, [string, string], [string, string], BuildComparison][] = [
    ['same release, tag spelled with v', ['0.6.0', 'unknown'], ['v0.6.0', 'unknown'], 'match'],
    ['same release, both bare', ['0.6.0', 'unknown'], ['0.6.0', 'unknown'], 'match'],
    ['different release', ['0.5.1', 'unknown'], ['v0.6.0', 'unknown'], 'mismatch'],
    [
      'dev-<sha> server, client knows no commit (#1273)',
      ['0.5.1', 'unknown'],
      ['dev-abc1234', 'abc1234'],
      'unknown',
    ],
    [
      'dev-<sha> server without git_sha, client knows no commit',
      ['0.5.1', 'unknown'],
      ['dev-abc1234', 'unknown'],
      'unknown',
    ],
    ['missing server identity', ['0.5.1', 'unknown'], ['', ''], 'unknown'],
    ['unknown server identity', ['0.5.1', 'unknown'], ['unknown', 'unknown'], 'unknown'],
    ['dev image, same commit', ['0.5.1', 'abc1234'], ['dev-abc1234', 'abc1234'], 'match'],
    [
      'dev image, commit only in version',
      ['0.5.1', 'abc1234'],
      ['dev-abc1234', 'unknown'],
      'match',
    ],
    ['dev image, uppercase', ['0.5.1', 'abc1234'], ['DEV-ABC1234', 'unknown'], 'match'],
    ['dev image, other commit', ['0.5.1', 'abc1234'], ['dev-def5678', 'def5678'], 'mismatch'],
    ['same commit, different tag', ['0.6.0', 'abc1234'], ['v0.6.1', 'abc1234'], 'match'],
    [
      'same tag, rebuilt from another commit',
      ['0.6.0', 'abc1234'],
      ['v0.6.0', 'def5678'],
      'mismatch',
    ],
    ['short client sha, full server sha', ['0.6.0', 'abc1234'], ['v0.6.0', FULL_SHA], 'match'],
    ['full client sha, short server sha', ['0.6.0', FULL_SHA], ['v0.6.0', 'abc1234'], 'match'],
    ['dirty client commit', ['0.6.0', 'abc1234-dirty'], ['v0.6.0', 'abc1234'], 'match'],
    ['dirty server commit', ['0.6.0', 'abc1234'], ['v0.6.0', 'abc1234-dirty'], 'match'],
    [
      'client commit only, server release without commit',
      ['0.6.0', 'abc1234'],
      ['v0.6.0', 'unknown'],
      'match',
    ],
    [
      'client commit only, other release without commit',
      ['0.5.1', 'abc1234'],
      ['v0.6.0', 'unknown'],
      'mismatch',
    ],
  ];

  it.each(cases)('%s', (_name, [cv, cc], [sv, sc], expected) => {
    expect(compareBuilds({ version: cv, commit: cc }, { version: sv, commit: sc })).toBe(expected);
  });

  it('treats null and undefined fields as missing', () => {
    expect(compareBuilds({ version: '0.5.1' }, { version: null, commit: null })).toBe('unknown');
    expect(compareBuilds({}, {})).toBe('unknown');
  });
});

describe('sourceRefFor', () => {
  it.each([
    [
      { version: 'v0.6.0', commit: 'abc1234' },
      { kind: 'commit', sha: 'abc1234', exact: true },
    ],
    [
      { version: 'dev-abc1234', commit: 'abc1234' },
      { kind: 'commit', sha: 'abc1234', exact: true },
    ],
    [
      { version: 'dev-abc1234', commit: 'unknown' },
      { kind: 'commit', sha: 'abc1234', exact: true },
    ],
    [
      { version: 'dev-ABC1234', commit: '' },
      { kind: 'commit', sha: 'abc1234', exact: true },
    ],
    [
      { version: 'dev-abc1234', commit: 'abc1234-dirty' },
      { kind: 'commit', sha: 'abc1234', exact: false },
    ],
    [
      { version: 'v0.5.0', commit: 'unknown' },
      { kind: 'release', tag: 'v0.5.0' },
    ],
    [
      { version: '0.5.0', commit: null },
      { kind: 'release', tag: 'v0.5.0' },
    ],
    [{ version: 'unknown', commit: 'unknown' }, { kind: 'none' }],
    [{ version: '', commit: '' }, { kind: 'none' }],
    [{}, { kind: 'none' }],
  ])('%j -> %j', (server, expected) => {
    expect(sourceRefFor(server)).toEqual(expected);
  });

  it('never invents a vdev-<sha> release tag', () => {
    const ref = sourceRefFor({ version: 'dev-abc1234', commit: 'unknown' });
    expect(ref.kind).not.toBe('release');
  });
});
