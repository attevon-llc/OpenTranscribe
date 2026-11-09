/**
 * View state for the pyannote.ai API key form (issue #1204), kept out of the component so
 * the rules are testable without rendering it. Every value comes from the server's
 * `PyannoteCredentialStatus`; nothing here decides what the backend allows.
 */
import type { PyannoteCredentialStatus, PyannoteTestCode } from '$lib/api/pyannoteCredential';

const KEY_PREFIX = 'settings.speakerIdentification.pyannoteKey';

type PyannoteBadge = 'locked' | 'notConfigured' | 'configured' | 'verified' | 'failed';

export interface PyannoteCredentialView {
  badge: PyannoteBadge;
  /** i18n key for the status badge. */
  badgeKey: string;
  /** The key input and Save button are usable. */
  canSave: boolean;
  /** Label for the save button: Save the first key, Replace an existing one. */
  saveLabelKey: string;
  canDelete: boolean;
  /** Testing the SAVED key (an unsaved one can be tested whenever `canSave`). */
  canTestSaved: boolean;
  /** i18n key of a warning to show, or null. */
  warningKey: string | null;
}

const BADGE_SUFFIX: Record<PyannoteBadge, string> = {
  locked: 'statusLocked',
  notConfigured: 'statusNotConfigured',
  configured: 'statusConfigured',
  verified: 'statusVerified',
  failed: 'statusFailed',
};

function badgeFor(
  status: PyannoteCredentialStatus | null,
  locked: boolean,
  configured: boolean
): PyannoteBadge {
  if (locked) return 'locked';
  if (!configured) return 'notConfigured';
  if (status?.test_status === 'success') return 'verified';
  return status?.test_status === 'failed' ? 'failed' : 'configured';
}

function warningKeyFor(
  status: PyannoteCredentialStatus | null,
  locked: boolean,
  configured: boolean,
  diarizationSource: string
): string | null {
  if (locked) return `${KEY_PREFIX}.lockedHint`;
  if (status !== null && diarizationSource === 'pyannote' && !configured) {
    return `${KEY_PREFIX}.missingWarning`;
  }
  return null;
}

/**
 * @param status the server's status, or null while it is loading or after a failed load
 * @param diarizationSource the user's current speaker-detection source
 */
export function pyannoteCredentialView(
  status: PyannoteCredentialStatus | null,
  diarizationSource: string
): PyannoteCredentialView {
  const locked = status?.locked === true;
  const configured = status?.configured === true;

  const badge = badgeFor(status, locked, configured);
  const warningKey = warningKeyFor(status, locked, configured, diarizationSource);

  return {
    badge,
    badgeKey: `${KEY_PREFIX}.${BADGE_SUFFIX[badge]}`,
    canSave: status !== null && !locked,
    saveLabelKey: `${KEY_PREFIX}.${configured ? 'replace' : 'save'}`,
    // Removing your own secret is allowed even when the deployment locks the source.
    canDelete: configured,
    canTestSaved: configured && !locked,
    warningKey,
  };
}

/** Translated copy for a connection-test outcome; the server's `message` is English-only. */
export function pyannoteTestResultKey(code: PyannoteTestCode): string {
  const suffix: Record<PyannoteTestCode, string> = {
    connected: 'testSuccess',
    rejected: 'testRejected',
    unreachable: 'testUnreachable',
    error: 'testFailed',
  };
  return `${KEY_PREFIX}.${suffix[code]}`;
}

/** Copy for a failed save, by HTTP status: 422 bad shape, 409 locked, anything else generic. */
export function pyannoteSaveErrorKey(httpStatus: number | undefined): string {
  if (httpStatus === 422) return `${KEY_PREFIX}.invalidFormat`;
  if (httpStatus === 409) return `${KEY_PREFIX}.lockedHint`;
  return `${KEY_PREFIX}.saveFailed`;
}

/** Message after a delete; says so when the source was switched back to the default. */
export function pyannoteDeletedKey(sourceReverted: boolean): string {
  return `${KEY_PREFIX}.${sourceReverted ? 'deletedReverted' : 'deleted'}`;
}
