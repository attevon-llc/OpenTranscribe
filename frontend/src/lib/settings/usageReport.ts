/**
 * Pure view rules for `UsageReportCard` (issue #1019). The server decides what forces the report
 * off; this only maps its stable codes to translated copy and picks the status chip.
 */

import type { TelemetryOffReason, TelemetryStatus } from '$lib/api/telemetry';

export type UsageReportBadge = 'on' | 'off' | 'blocked';

const REASON_KEYS: Record<TelemetryOffReason, string> = {
  do_not_track: 'settings.usageReport.reason.doNotTrack',
  env_disabled: 'settings.usageReport.reason.envDisabled',
  demo: 'settings.usageReport.reason.demo',
  no_receiver: 'settings.usageReport.reason.noReceiver',
  offline: 'settings.usageReport.reason.offline',
  pki: 'settings.usageReport.reason.pki',
  fips: 'settings.usageReport.reason.fips',
};

/** Translation key for each reason the server gave; an unknown code still says "forced off". */
export function usageReportReasonKeys(reasons: readonly string[]): string[] {
  return reasons.map(
    (reason) => REASON_KEYS[reason as TelemetryOffReason] ?? 'settings.usageReport.reason.unknown'
  );
}

/** The chip: forced off wins over the switch, so a stored "on" never reads as sending. */
export function usageReportBadge(status: TelemetryStatus | null): UsageReportBadge {
  if (!status) return 'off';
  if (status.off_reasons.length > 0) return 'blocked';
  return status.sending ? 'on' : 'off';
}

const BADGE_KEYS: Record<UsageReportBadge, string> = {
  on: 'settings.usageReport.status.on',
  off: 'settings.usageReport.status.off',
  blocked: 'settings.usageReport.status.blocked',
};

/** Translation key for the chip. */
export function usageReportBadgeKey(badge: UsageReportBadge): string {
  return BADGE_KEYS[badge];
}

/** Pretty-printed preview, exactly as the server returned it. */
export function usageReportPreviewText(status: TelemetryStatus | null): string {
  return status ? JSON.stringify(status.preview, null, 2) : '';
}
