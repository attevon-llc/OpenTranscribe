import { describe, it, expect } from 'vitest';
import { hfTokenView, hfTokenTestKey, hfTokenSaveErrorKey } from './hfToken';

const P = 'settings.engineSettings.hfToken';

describe('hfTokenView', () => {
  it('is unknown while the status is not loaded', () => {
    const v = hfTokenView(null);
    expect(v.badge).toBe('unknown');
    expect(v.badgeKey).toBe(`${P}.statusUnknown`);
    expect(v.canRemove).toBe(false);
    expect(v.canTestInEffect).toBe(false);
  });

  it('is "not set" when nothing is in effect, even if the source says app', () => {
    const v = hfTokenView({ is_set: false, source: 'none' });
    expect(v.badge).toBe('none');
    expect(v.saveLabelKey).toBe(`${P}.save`);
    expect(v.canRemove).toBe(false);
    expect(v.canTestInEffect).toBe(false);
    expect(v.noteKey).toBeNull();
  });

  it('a token saved in Settings can be replaced, removed and tested', () => {
    const v = hfTokenView({ is_set: true, source: 'app' });
    expect(v.badge).toBe('app');
    expect(v.saveLabelKey).toBe(`${P}.replace`);
    expect(v.canRemove).toBe(true);
    expect(v.canTestInEffect).toBe(true);
    expect(v.noteKey).toBeNull();
  });

  it('a token from the server environment cannot be removed here but explains itself', () => {
    const v = hfTokenView({ is_set: true, source: 'environment' });
    expect(v.badge).toBe('environment');
    expect(v.canRemove).toBe(false);
    expect(v.canTestInEffect).toBe(true);
    expect(v.saveLabelKey).toBe(`${P}.save`);
    expect(v.noteKey).toBe(`${P}.environmentNote`);
  });
});

describe('copy keys', () => {
  it('maps every test code to its own sentence', () => {
    expect(hfTokenTestKey('valid')).toBe(`${P}.testValid`);
    expect(hfTokenTestKey('invalid')).toBe(`${P}.testInvalid`);
    expect(hfTokenTestKey('unreachable')).toBe(`${P}.testUnreachable`);
    expect(hfTokenTestKey('error')).toBe(`${P}.testFailed`);
  });

  it('chooses the save error from the status only', () => {
    expect(hfTokenSaveErrorKey(422)).toBe(`${P}.saveInvalid`);
    expect(hfTokenSaveErrorKey(403)).toBe(`${P}.saveForbidden`);
    expect(hfTokenSaveErrorKey(500)).toBe(`${P}.saveFailed`);
    expect(hfTokenSaveErrorKey(undefined)).toBe(`${P}.saveFailed`);
  });
});
