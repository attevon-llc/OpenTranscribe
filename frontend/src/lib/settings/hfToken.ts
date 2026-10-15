/**
 * View state for the Hugging Face access token card, kept out of the component so the rules
 * are testable without rendering it. Every value comes from the server's `HfTokenStatus`.
 */
import type { HfTokenStatus, HfTokenTestCode } from '$lib/api/hfToken';

const P = 'settings.engineSettings.hfToken';

export type HfTokenBadge = 'app' | 'environment' | 'none' | 'unknown';

export interface HfTokenView {
  badge: HfTokenBadge;
  /** i18n key for the status chip. */
  badgeKey: string;
  /** Save (or replace) the token. */
  saveLabelKey: string;
  /** Only a token saved in Settings can be removed; the server environment's cannot. */
  canRemove: boolean;
  /** Testing the token already in effect (an unsaved one can be tested whenever typed). */
  canTestInEffect: boolean;
  /** A line explaining where the token in effect comes from, or null. */
  noteKey: string | null;
}

/** @param status the server's status, or null while loading or after a failed load */
export function hfTokenView(status: HfTokenStatus | null): HfTokenView {
  const badge: HfTokenBadge = status === null ? 'unknown' : status.is_set ? status.source : 'none';
  const badgeSuffix: Record<HfTokenBadge, string> = {
    app: 'statusApp',
    environment: 'statusEnvironment',
    none: 'statusNone',
    unknown: 'statusUnknown',
  };
  return {
    badge,
    badgeKey: `${P}.${badgeSuffix[badge]}`,
    saveLabelKey: badge === 'app' ? `${P}.replace` : `${P}.save`,
    canRemove: badge === 'app',
    canTestInEffect: badge === 'app' || badge === 'environment',
    noteKey: badge === 'environment' ? `${P}.environmentNote` : null,
  };
}

const TEST_KEYS: Record<HfTokenTestCode, string> = {
  valid: `${P}.testValid`,
  invalid: `${P}.testInvalid`,
  unreachable: `${P}.testUnreachable`,
};

export function hfTokenTestKey(code: HfTokenTestCode | 'error'): string {
  return code === 'error' ? `${P}.testFailed` : TEST_KEYS[code];
}

/** Toast copy for a failed save, from the HTTP status only (never the response body). */
export function hfTokenSaveErrorKey(status: number | undefined): string {
  if (status === 422) return `${P}.saveInvalid`;
  if (status === 403) return `${P}.saveForbidden`;
  return `${P}.saveFailed`;
}
