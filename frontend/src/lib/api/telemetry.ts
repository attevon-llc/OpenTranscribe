/**
 * The opt-in anonymous usage report (issue #1019, super_admin only).
 *
 * Mirrors `backend/app/schemas/telemetry.py`. `preview` is the exact request body the server
 * would send, built by the same function that sends it. Turning the report on is refused with
 * 409 `{code: 'telemetry_refused', reasons}` while anything in `off_reasons` applies.
 */

import axiosInstance from '$lib/axios';

const BASE = '/admin/telemetry';

export type TelemetryOffReason = 'do_not_track' | 'env_disabled' | 'demo' | 'no_receiver';

export interface TelemetryStatus {
  /** The administrator's choice. */
  enabled: boolean;
  /** True only when enabled and nothing forces it off. */
  sending: boolean;
  off_reasons: TelemetryOffReason[];
  last_sent_at: string | null;
  endpoint: string;
  preview: Record<string, unknown>;
}

export async function getTelemetryStatus(): Promise<TelemetryStatus> {
  const response = await axiosInstance.get(BASE);
  return response.data;
}

export async function setTelemetryEnabled(enabled: boolean): Promise<TelemetryStatus> {
  const response = await axiosInstance.put(BASE, { enabled });
  return response.data;
}
