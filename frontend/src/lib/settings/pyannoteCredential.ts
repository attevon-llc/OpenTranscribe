/**
 * View state for the pyannote.ai API key form (issue #1204), kept out of the component so
 * the rules are testable without rendering it. Every value comes from the server's
 * `PyannoteCredentialStatus`; nothing here decides what the backend allows.
 */
import type { PyannoteCredentialStatus, PyannoteTestCode } from '$lib/api/pyannoteCredential';

const KEY_PREFIX = 'settings.speakerIdentification.pyannoteKey';

export type PyannoteBadge = 'locked' | 'notConfigured' | 'configured' | 'verified' | 'failed';

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

  let badge: PyannoteBadge;
  if (locked) badge = 'locked';
  else if (!configured) badge = 'notConfigured';
  else if (status?.test_status === 'success') badge = 'verified';
  else if (status?.test_status === 'failed') badge = 'failed';
  else badge = 'configured';

  const badgeSuffix: Record<PyannoteBadge, string> = {
    locked: 'statusLocked',
    notConfigured: 'statusNotConfigured',
    configured: 'statusConfigured',
    verified: 'statusVerified',
    failed: 'statusFailed',
  };

  let warningKey: string | null = null;
  if (locked) warningKey = `${KEY_PREFIX}.lockedHint`;
  else if (status !== null && diarizationSource === 'pyannote' && !configured)
    warningKey = `${KEY_PREFIX}.missingWarning`;

  return {
    badge,
    badgeKey: `${KEY_PREFIX}.${badgeSuffix[badge]}`,
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

/**
 * Copy for `PUT /user-settings/transcription` refusing `diarization_source: 'pyannote'`
 * with 409 because no key is saved; null for any other failure.
 */
export function pyannoteSelectionErrorKey(
  httpStatus: number | undefined,
  requestedSource: string
): string | null {
  return httpStatus === 409 && requestedSource === 'pyannote'
    ? `${KEY_PREFIX}.requiredToSelect`
    : null;
}

/** Message after a delete; says so when the source was switched back to the default. */
export function pyannoteDeletedKey(sourceReverted: boolean): string {
  return `${KEY_PREFIX}.${sourceReverted ? 'deletedReverted' : 'deleted'}`;
}
