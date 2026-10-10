/**
 * #1204: the pyannote.ai key form's view rules, plus a check that every i18n key the helpers
 * can return exists in the real `en` bundle (a raw dot-key on screen is the failure shape).
 */
import { describe, it, expect, beforeAll } from 'vitest';
import i18next from 'i18next';
import { initI18n } from '$lib/i18n';
import type { PyannoteCredentialStatus, PyannoteTestCode } from '$lib/api/pyannoteCredential';
import {
  pyannoteCredentialView,
  pyannoteDeletedKey,
  pyannoteSaveErrorKey,
  pyannoteSelectionErrorKey,
  pyannoteTestResultKey,
} from './pyannoteCredential';

function status(overrides: Partial<PyannoteCredentialStatus> = {}): PyannoteCredentialStatus {
  return {
    configured: false,
    locked: false,
    test_status: null,
    test_message: null,
    last_tested: null,
    updated_at: null,
    ...overrides,
  };
}

describe('pyannoteCredentialView', () => {
  it('offers Save and warns when pyannote is selected with no key', () => {
    const view = pyannoteCredentialView(status(), 'pyannote');
    expect(view.badge).toBe('notConfigured');
    expect(view.canSave).toBe(true);
    expect(view.saveLabelKey).toBe('settings.speakerIdentification.pyannoteKey.save');
    expect(view.canDelete).toBe(false);
    expect(view.canTestSaved).toBe(false);
    expect(view.warningKey).toBe('settings.speakerIdentification.pyannoteKey.missingWarning');
  });

  it('does not warn about a missing key when another source is selected', () => {
    expect(pyannoteCredentialView(status(), 'provider').warningKey).toBeNull();
  });

  it('offers Replace, Remove and Test once a key is saved', () => {
    const view = pyannoteCredentialView(status({ configured: true }), 'pyannote');
    expect(view.badge).toBe('configured');
    expect(view.saveLabelKey).toBe('settings.speakerIdentification.pyannoteKey.replace');
    expect(view.canDelete).toBe(true);
    expect(view.canTestSaved).toBe(true);
    expect(view.warningKey).toBeNull();
  });

  it.each([
    ['success', 'verified'],
    ['failed', 'failed'],
  ] as const)('a saved key whose last test was %s shows %s', (testStatus, badge) => {
    const view = pyannoteCredentialView(
      status({ configured: true, test_status: testStatus }),
      'pyannote'
    );
    expect(view.badge).toBe(badge);
  });

  it('locks saving and testing but still allows removing your own key', () => {
    const view = pyannoteCredentialView(status({ configured: true, locked: true }), 'provider');
    expect(view.badge).toBe('locked');
    expect(view.canSave).toBe(false);
    expect(view.canTestSaved).toBe(false);
    expect(view.canDelete).toBe(true);
    expect(view.warningKey).toBe('settings.speakerIdentification.pyannoteKey.lockedHint');
  });

  it('disables everything while the status is unknown, without a false warning', () => {
    const view = pyannoteCredentialView(null, 'pyannote');
    expect(view.canSave).toBe(false);
    expect(view.canDelete).toBe(false);
    expect(view.canTestSaved).toBe(false);
    expect(view.warningKey).toBeNull();
  });
});

describe('error and result copy', () => {
  it('maps save failures by status', () => {
    expect(pyannoteSaveErrorKey(422)).toBe(
      'settings.speakerIdentification.pyannoteKey.invalidFormat'
    );
    expect(pyannoteSaveErrorKey(409)).toBe('settings.speakerIdentification.pyannoteKey.lockedHint');
    expect(pyannoteSaveErrorKey(500)).toBe('settings.speakerIdentification.pyannoteKey.saveFailed');
    expect(pyannoteSaveErrorKey(undefined)).toBe(
      'settings.speakerIdentification.pyannoteKey.saveFailed'
    );
  });

  it('recognises only the pyannote selection refusal', () => {
    expect(pyannoteSelectionErrorKey(409, 'pyannote')).toBe(
      'settings.speakerIdentification.pyannoteKey.requiredToSelect'
    );
    expect(pyannoteSelectionErrorKey(409, 'local')).toBeNull();
    expect(pyannoteSelectionErrorKey(422, 'pyannote')).toBeNull();
  });

  it('says when a delete reverted the source', () => {
    expect(pyannoteDeletedKey(true)).toBe(
      'settings.speakerIdentification.pyannoteKey.deletedReverted'
    );
    expect(pyannoteDeletedKey(false)).toBe('settings.speakerIdentification.pyannoteKey.deleted');
  });
});

describe('every key the helpers return is translated in en', () => {
  beforeAll(async () => {
    await initI18n('en');
  });

  const codes: PyannoteTestCode[] = ['connected', 'rejected', 'unreachable', 'error'];
  const views = [
    pyannoteCredentialView(status(), 'pyannote'),
    pyannoteCredentialView(status({ configured: true }), 'pyannote'),
    pyannoteCredentialView(status({ configured: true, test_status: 'success' }), 'pyannote'),
    pyannoteCredentialView(status({ configured: true, test_status: 'failed' }), 'pyannote'),
    pyannoteCredentialView(status({ locked: true }), 'pyannote'),
  ];
  const keys = [
    ...views.flatMap((v) => [v.badgeKey, v.saveLabelKey, v.warningKey]),
    ...codes.map(pyannoteTestResultKey),
    pyannoteSaveErrorKey(422),
    pyannoteSaveErrorKey(500),
    pyannoteSelectionErrorKey(409, 'pyannote'),
    pyannoteDeletedKey(true),
    pyannoteDeletedKey(false),
  ].filter((key): key is string => key !== null);

  it('covers every badge, label, warning and message', () => {
    expect(new Set(keys).size).toBe(18);
  });

  it.each([...new Set(keys)])('%s renders text', (key) => {
    const text = i18next.t(key);
    expect(text).not.toBe(key);
    expect(text.length).toBeGreaterThan(0);
  });
});
