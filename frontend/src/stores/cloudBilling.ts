/**
 * Cloud-edition billing + usage stores.
 *
 * Hold the org's subscription state and metered usage so the navbar badge,
 * upgrade CTA, usage dashboard, and pre-flight quota check can all subscribe to
 * one source of truth. Populated by `refreshUsage()` / `refreshBilling()`, which
 * hit the cloud-only `/api/billing/*` routes.
 *
 * Community/self-host never calls these (every caller is gated by `isCloudEdition`
 * + capability), so the stores simply stay at their empty defaults there.
 */
import { writable } from 'svelte/store';
import { BillingApi } from '$lib/api/billing';

export interface UsageState {
  loaded: boolean;
  hours_used: number;
  /** Plan limit in hours; null = unmetered/unlimited. */
  limit: number | null;
  remaining: number | null;
  files_this_month: number;
}

export interface BillingState {
  loaded: boolean;
  plan: string;
  status: string;
  next_billing_date: string | null;
}

const USAGE_DEFAULTS: UsageState = {
  loaded: false,
  hours_used: 0,
  limit: null,
  remaining: null,
  files_this_month: 0,
};

const BILLING_DEFAULTS: BillingState = {
  loaded: false,
  plan: '',
  status: '',
  next_billing_date: null,
};

export const usageStore = writable<UsageState>(USAGE_DEFAULTS);
export const billingStore = writable<BillingState>(BILLING_DEFAULTS);

/** Fetch org usage and update {@link usageStore}. Safe to call repeatedly. */
export async function refreshUsage(): Promise<void> {
  const usage = await BillingApi.getUsage();
  usageStore.set({
    loaded: true,
    hours_used: usage.hours_used ?? 0,
    limit: usage.limit ?? null,
    remaining: usage.remaining ?? null,
    files_this_month: usage.files_this_month ?? 0,
  });
}

/** Fetch subscription state and update {@link billingStore}. Safe to call repeatedly. */
export async function refreshBilling(): Promise<void> {
  const sub = await BillingApi.getSubscription();
  billingStore.set({
    loaded: true,
    plan: sub.plan ?? '',
    status: sub.status ?? '',
    next_billing_date: sub.next_billing_date ?? null,
  });
}

/**
 * Fraction of the quota consumed (0..1+), or null when unmetered (`limit` is
 * null/0). >1 means over the limit (overage).
 */
export function usageFraction(state: UsageState): number | null {
  if (!state.limit || state.limit <= 0) return null;
  return state.hours_used / state.limit;
}

/** True when usage is at/over 80% of the plan limit (drives the upgrade CTA). */
export function isNearLimit(state: UsageState): boolean {
  const frac = usageFraction(state);
  return frac !== null && frac >= 0.8;
}

/** True when usage is at/over the plan limit. */
export function isOverLimit(state: UsageState): boolean {
  const frac = usageFraction(state);
  return frac !== null && frac >= 1;
}
