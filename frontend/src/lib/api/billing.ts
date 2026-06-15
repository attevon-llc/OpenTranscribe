/**
 * Billing / usage API client (cloud edition only).
 *
 * Thin wrappers around the cloud backend's billing routes. These endpoints only
 * exist in the cloud edition; the community build never imports/calls them (all
 * callers are gated by `isCloudEdition` and the `cap:billing` / `cap:usage_dashboard`
 * capabilities). The backend independently 404s these in community.
 *
 * IMPORTANT: use the real route names — `subscription`, `usage`, `checkout-session`,
 * `portal-session` (NOT `plan` / `checkout` / `portal`). The checkout/portal endpoints
 * accept redirect URLs that the backend (`billing.py`) open-redirect-guards.
 */
import axiosInstance from '$lib/axios';

/** Subscription / plan state for the current organization. */
export interface BillingSubscription {
  plan: string;
  status: string;
  next_billing_date: string | null;
  /** Per-hour (or per-unit) overage rate, if the plan allows overage. */
  overage_rate: number | null;
  /** Hours included in the plan before overage applies. */
  included_hours: number | null;
  /** Seats included in the plan (team size cap). */
  seats_limit: number | null;
  /** Whether the org can purchase usage beyond `included_hours`. */
  overage_enabled: boolean;
}

/** Per-member usage breakdown row. */
export interface UsageMember {
  user_uuid: string;
  email: string;
  full_name: string | null;
  hours_used: number;
  files_count: number;
}

/** Org-wide usage for the current billing period. */
export interface BillingUsage {
  hours_used: number;
  /** Plan limit in hours; null = unmetered/unlimited. */
  limit: number | null;
  remaining: number | null;
  files_this_month: number;
  period_start: string | null;
  period_end: string | null;
  members: UsageMember[];
}

export class BillingApi {
  /** GET /api/billing/subscription — plan, status, next billing date, overage. */
  static async getSubscription(): Promise<BillingSubscription> {
    const response = await axiosInstance.get('/billing/subscription');
    return response.data;
  }

  /** GET /api/billing/usage — org usage + per-member breakdown. */
  static async getUsage(): Promise<BillingUsage> {
    const response = await axiosInstance.get('/billing/usage');
    return response.data;
  }

  /**
   * POST /api/billing/checkout-session — start a Stripe Checkout flow.
   * The backend open-redirect-guards `success_url` / `cancel_url`.
   */
  static async createCheckoutSession(opts: {
    success_url: string;
    cancel_url: string;
    plan?: string;
  }): Promise<{ url: string }> {
    const response = await axiosInstance.post('/billing/checkout-session', opts);
    return response.data;
  }

  /**
   * POST /api/billing/portal-session — open the Stripe Customer Portal.
   * The backend open-redirect-guards `return_url`.
   */
  static async createPortalSession(opts: { return_url: string }): Promise<{ url: string }> {
    const response = await axiosInstance.post('/billing/portal-session', opts);
    return response.data;
  }
}
