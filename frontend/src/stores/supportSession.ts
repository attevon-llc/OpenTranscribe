/**
 * The support-access session: "this tab is acting inside someone else's workspace" (issue #1122).
 *
 * Persisted in `sessionStorage`, not `localStorage`, on purpose: a grant is per tab and must
 * not become ambient authority in the user's other tabs. Only the grant uuid is stored; the
 * grant is re-validated against the server on every restore.
 *
 * The header itself lives in `$lib/supportAccess/headers` (no store imports, so axios can
 * depend on it); this store is the only writer of it.
 */
import { derived, get, writable } from 'svelte/store';
import { goto } from '$app/navigation';
import {
  SupportAccessApi,
  type AccessLevel,
  type GrantMode,
  type SupportGrant,
  type TargetKind,
} from '$lib/api/supportAccess';
import { SUPPORT_SESSION_END_KEYS } from '$lib/i18n/keyMaps';
import { purgeTenantDataCaches } from '$lib/session/clearUserState';
import { setActiveSupportGrant } from '$lib/supportAccess/headers';
import { capabilities } from '$stores/capabilities';
import { t } from '$stores/locale';
import { toastStore } from '$stores/toast';

const STORAGE_KEY = 'opentr:supportSession';

export type EndReason = 'user' | 'expired' | 'revoked' | 'invalid' | 'logout';

export interface SupportSessionState {
  active: boolean;
  grantUuid: string | null;
  targetLabel: string;
  targetKind: TargetKind;
  level: AccessLevel;
  mode: GrantMode;
  expiresAtMs: number;
  /** server clock minus client clock, so a skewed laptop still counts down correctly. */
  skewMs: number;
  remainingSeconds: number;
}

const INACTIVE: SupportSessionState = {
  active: false,
  grantUuid: null,
  targetLabel: '',
  targetKind: 'organization',
  level: 'read',
  mode: 'approved',
  expiresAtMs: 0,
  skewMs: 0,
  remainingSeconds: 0,
};

const state = writable<SupportSessionState>(INACTIVE);
let timer: ReturnType<typeof setInterval> | null = null;

function stopTimer(): void {
  if (timer !== null) clearInterval(timer);
  timer = null;
}

function readStored(): string | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as { grantUuid?: unknown }) : null;
    return typeof parsed?.grantUuid === 'string' ? parsed.grantUuid : null;
  } catch {
    return null;
  }
}

function writeStored(uuid: string | null): void {
  try {
    if (uuid) sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ grantUuid: uuid }));
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage blocked: the session still works, it just will not survive a reload.
  }
}

function targetLabelOf(grant: SupportGrant): string {
  const tr = get(t);
  if (grant.target_kind === 'organization') {
    return grant.organization?.name ?? tr('supportAccess.deletedOrganization');
  }
  const user = grant.subject_user;
  return user ? user.full_name || user.email : tr('supportAccess.deletedUser');
}

function remainingOf(s: SupportSessionState): number {
  return Math.max(0, Math.ceil((s.expiresAtMs - (Date.now() + s.skewMs)) / 1000));
}

function tick(): void {
  const current = get(state);
  if (!current.active) return;
  const remainingSeconds = remainingOf(current);
  if (remainingSeconds <= 0) {
    void end('expired');
    return;
  }
  state.set({ ...current, remainingSeconds });
}

/**
 * Fetch the grant plus the server's clock. The detail route carries no `server_time` (only
 * list pages do), so the skew comes from a one-row page fetched alongside it; if that fails
 * the skew is 0 and the server stays authoritative through the session-ending 403s.
 */
async function load(uuid: string): Promise<{ grant: SupportGrant; skewMs: number }> {
  const [grant, page] = await Promise.all([
    SupportAccessApi.getGrant(uuid),
    SupportAccessApi.listMyGrants({ scope: 'mine', limit: 1, offset: 0 }).catch(() => null),
  ]);
  const serverMs = page ? Date.parse(page.server_time) : NaN;
  return { grant, skewMs: Number.isNaN(serverMs) ? 0 : serverMs - Date.now() };
}

async function start(uuid: string, redirect: boolean): Promise<boolean> {
  if (get(capabilities).tenancyMode !== 'multi') return false;
  let loaded: { grant: SupportGrant; skewMs: number };
  try {
    loaded = await load(uuid);
  } catch {
    return false;
  }
  const { grant, skewMs } = loaded;
  const expiresAtMs = grant.expires_at ? Date.parse(grant.expires_at) : NaN;
  if (grant.status !== 'active' || Number.isNaN(expiresAtMs)) return false;

  stopTimer();
  if (redirect) await purgeTenantDataCaches();
  const next: SupportSessionState = {
    active: true,
    grantUuid: grant.uuid,
    targetLabel: targetLabelOf(grant),
    targetKind: grant.target_kind,
    level: grant.access_level,
    mode: grant.grant_mode,
    expiresAtMs,
    skewMs,
    remainingSeconds: 0,
  };
  next.remainingSeconds = remainingOf(next);
  // The header goes live BEFORE the store flips: the layout remounts the page when `grantUuid`
  // changes, and that page's first requests must already carry (or have dropped) the grant.
  setActiveSupportGrant(grant.uuid);
  writeStored(grant.uuid);
  state.set(next);
  timer = setInterval(tick, 1000);
  if (redirect) await goto('/');
  return true;
}

/** Begin acting under `grantUuid`. Returns false (after a toast) when it cannot start. */
async function activate(grantUuid: string): Promise<boolean> {
  if (get(capabilities).tenancyMode !== 'multi') return false;
  const ok = await start(grantUuid, true);
  if (!ok) toastStore.error(get(t)('supportAccess.session.activateFailed'));
  return ok;
}

let ending = false;

/** Stop acting under the grant. Idempotent: a second call is a no-op. */
async function end(reason: EndReason): Promise<void> {
  if (!get(state).active) {
    // Nothing live, but a stale pointer must still go (logout, failed restore).
    writeStored(null);
    return;
  }
  if (ending) return;
  ending = true;
  try {
    stopTimer();
    setActiveSupportGrant(null);
    writeStored(null);
    // Purge BEFORE the store flips: flipping remounts the page, and a purge that landed after
    // its first fetches would clear what the new scope had just loaded.
    await purgeTenantDataCaches();
    state.set(INACTIVE);
  } finally {
    ending = false;
  }
  if (reason !== 'logout') {
    const message = get(t)(SUPPORT_SESSION_END_KEYS[reason]);
    if (reason === 'user') toastStore.info(message);
    else toastStore.error(message);
    await goto('/');
  }
}

/**
 * Re-establish this tab's session after a reload. Silent on any failure: a revoked,
 * expired or unreachable grant simply leaves the tab in its own workspace.
 */
async function restore(): Promise<void> {
  const uuid = readStored();
  if (!uuid) return;
  if (!(await start(uuid, false))) writeStored(null);
}

export const supportSession = { subscribe: state.subscribe, activate, end, restore };

/** The one derived gate every hidden surface reads (design section 3.4). */
export const supportSessionGate = derived(state, ($s) => ({
  active: $s.active,
  readOnly: $s.active && $s.level === 'read',
}));
