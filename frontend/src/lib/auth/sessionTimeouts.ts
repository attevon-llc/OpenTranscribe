/**
 * Session timeouts for sessions owned by an external identity provider (issue #1106).
 *
 * Built-in sessions are timed out server-side on their refresh-token row and never
 * start this. An external provider keeps its own browser session alive and refreshes
 * tokens silently, so the browser has to notice the person has gone: this wires
 * `idleGuard` to the configured limits (`GET /auth/methods`), the `$lib/cloud`
 * auth-adapter hooks, the upload tray and the logout path.
 *
 * On expiry the UI locks at once (AC-11: conceal what was on screen). Sign-out waits
 * for in-flight uploads, because logout aborts them, but never longer than
 * UPLOAD_GRACE_MS.
 */
import { get, writable, type Readable } from 'svelte/store';
import { goto } from '$app/navigation';
import { createIdleGuard, type IdleGuard, type IdleReason } from './idleGuard';

export type SessionLockState =
  | { phase: 'active' }
  | { phase: 'warning'; reason: IdleReason; deadline: number }
  | { phase: 'locked'; reason: IdleReason; waitingForUploads: boolean };

export const sessionLock = writable<SessionLockState>({ phase: 'active' });

/** Warn this long before a deadline (capped at half the idle limit). */
export const WARNING_LEAD_MS = 2 * 60_000;
/** Longest an in-flight upload may delay sign-out after the UI has locked. */
export const UPLOAD_GRACE_MS = 60 * 60_000;
/** First time this browser saw the current external session (epoch ms). */
export const FIRST_SEEN_KEY = 'opentr:externalSessionFirstSeen';

export interface SessionTimeoutConfig {
  idleMinutes: number;
  absoluteMinutes: number;
  /** Provider-reported authentication time, epoch seconds, or null if unknown. */
  authTimeSeconds: number | null;
}

export interface SessionTimeoutDeps {
  hasActiveUploads: Readable<boolean>;
  /** Ends the session for good. Defaults to {@link endExpiredSession}. */
  endSession: (reason: IdleReason) => Promise<void>;
}

let guard: IdleGuard | null = null;
let uploadWait: (() => void) | null = null;
let graceTimer: ReturnType<typeof setTimeout> | null = null;
let ending: Promise<void> | null = null;

function readFirstSeen(): number | null {
  try {
    const raw = Number(localStorage.getItem(FIRST_SEEN_KEY));
    return Number.isFinite(raw) && raw > 0 ? raw : null;
  } catch {
    return null;
  }
}

/**
 * When the session started, in epoch ms: the provider's `auth_time` when it reports
 * one, otherwise the first time this browser saw the session (persisted, so a reload
 * does not restart the absolute clock).
 */
export function resolveSessionStartMs(authTimeSeconds: number | null, now = Date.now()): number {
  if (
    typeof authTimeSeconds === 'number' &&
    Number.isFinite(authTimeSeconds) &&
    authTimeSeconds > 0
  ) {
    return authTimeSeconds * 1000;
  }
  const seen = readFirstSeen();
  if (seen !== null && seen <= now) return seen;
  try {
    localStorage.setItem(FIRST_SEEN_KEY, String(now));
  } catch {
    /* private mode: the absolute clock restarts per page load, the server still enforces */
  }
  return now;
}

function clearUploadWait(): void {
  uploadWait?.();
  uploadWait = null;
  if (graceTimer !== null) clearTimeout(graceTimer);
  graceTimer = null;
}

function onExpired(reason: IdleReason, deps: SessionTimeoutDeps): void {
  const finish = (): void => {
    clearUploadWait();
    void deps.endSession(reason);
  };
  if (!get(deps.hasActiveUploads)) {
    sessionLock.set({ phase: 'locked', reason, waitingForUploads: false });
    finish();
    return;
  }
  sessionLock.set({ phase: 'locked', reason, waitingForUploads: true });
  graceTimer = setTimeout(finish, UPLOAD_GRACE_MS);
  // The subscription fires synchronously with the current (true) value first.
  uploadWait = deps.hasActiveUploads.subscribe((active) => {
    if (!active && uploadWait) finish();
  });
}

/**
 * Start guarding the current session. Idempotent: a second call replaces the first.
 * Returns false (and starts nothing) when both limits are off.
 */
export function startSessionTimeouts(
  config: SessionTimeoutConfig,
  deps: SessionTimeoutDeps
): boolean {
  stopSessionTimeouts();
  const idleMs = Math.max(0, config.idleMinutes) * 60_000;
  const absoluteMs = Math.max(0, config.absoluteMinutes) * 60_000;
  if (idleMs === 0 && absoluteMs === 0) return false;

  const absoluteDeadline =
    absoluteMs > 0 ? resolveSessionStartMs(config.authTimeSeconds) + absoluteMs : null;
  const warningLeadMs = idleMs > 0 ? Math.min(WARNING_LEAD_MS, idleMs / 2) : WARNING_LEAD_MS;

  guard = createIdleGuard({
    idleTimeoutMs: idleMs,
    absoluteDeadline,
    warningLeadMs,
    onChange: (state) => {
      if (state.phase === 'expired') onExpired(state.reason, deps);
      else sessionLock.set(state);
    },
  });
  // createIdleGuard evaluates once synchronously; an already-past absolute deadline
  // expires before onChange could be observed by a subscriber, which is fine: the
  // lock store has already been set by then.
  return true;
}

/** Stop guarding and clear this session's state. Registered in clearUserState. */
export function stopSessionTimeouts(): void {
  guard?.stop();
  guard = null;
  clearUploadWait();
  sessionLock.set({ phase: 'active' });
}

/** The warning dialog's "Stay signed in". */
export function staySignedIn(): void {
  guard?.staySignedIn();
}

/**
 * The server refused the session as expired. With a guard running, lock and let it
 * finish (uploads, other tabs); without one (e.g. the very first request of a page
 * load), end the session directly.
 */
export function expireSessionNow(reason: IdleReason): void {
  if (guard) guard.expire(reason);
  else void endExpiredSession(reason);
}

/**
 * End the session after a timeout. Absolute: offer the auth adapter its own
 * re-authentication first; otherwise (and always for idle) sign out through the
 * normal logout path, which calls the adapter's `externalSignOut()`.
 */
export function endExpiredSession(reason: IdleReason): Promise<void> {
  // Many requests can be refused at once; end the session once.
  if (ending === null) {
    ending = endExpiredSessionOnce(reason).finally(() => {
      ending = null;
    });
  }
  return ending;
}

async function endExpiredSessionOnce(reason: IdleReason): Promise<void> {
  if (reason === 'absolute') {
    try {
      const adapter: Record<string, unknown> = await import('$lib/cloud');
      const reauth = adapter.externalReauthenticate;
      if (typeof reauth === 'function' && (await reauth(reason)) === true) return;
    } catch {
      /* fall through to a plain sign-out */
    }
  }
  const { logout } = await import('$stores/auth');
  await logout(reason === 'idle' ? 'idle_timeout' : 'absolute_timeout');
  try {
    localStorage.removeItem(FIRST_SEEN_KEY);
  } catch {
    /* ignore */
  }
  await goto('/login');
}

/**
 * Read the limits from `/auth/methods` and the session start from the adapter.
 * Never throws: a failed read leaves the guard off and the server backstop in place.
 */
export async function loadSessionTimeoutConfig(): Promise<SessionTimeoutConfig | null> {
  try {
    const { getAuthMethods } = await import('$stores/auth');
    const methods = await getAuthMethods();
    let authTimeSeconds: number | null = null;
    try {
      const adapter: Record<string, unknown> = await import('$lib/cloud');
      const fn = adapter.getExternalSessionAuthTime;
      if (typeof fn === 'function') {
        const value = await fn();
        authTimeSeconds = typeof value === 'number' ? value : null;
      }
    } catch {
      authTimeSeconds = null;
    }
    return {
      idleMinutes: Number(methods.session_idle_timeout_minutes) || 0,
      absoluteMinutes: Number(methods.session_absolute_timeout_minutes) || 0,
      authTimeSeconds,
    };
  } catch {
    return null;
  }
}
