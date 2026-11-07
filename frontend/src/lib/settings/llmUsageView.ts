/**
 * View rules for the AI usage panel (issue #790): how much of a token limit is used, and how
 * an estimated cost is shown. Pure, so the panel's tests can pin them without rendering.
 */

/** Percent of `limit` used, capped at 100; `null` when there is no limit (`0` = unlimited). */
export function percentUsed(used: number, limit: number): number | null {
  if (!limit || limit <= 0) return null;
  return Math.min(100, Math.max(0, (used / limit) * 100));
}

/** A token count in the reader's locale, e.g. `12,345`. */
export function formatTokens(tokens: number, locale?: string): string {
  return new Intl.NumberFormat(locale).format(Math.round(tokens));
}

/**
 * An estimated USD cost, or `null` when it cannot be shown: no rate is known, so a figure would
 * be a guess. A cost under one cent is shown with more precision rather than as $0.00.
 */
export function formatEstimatedCost(usd: number | null, locale?: string): string | null {
  if (usd === null || usd === undefined || Number.isNaN(usd)) return null;
  const digits = usd > 0 && usd < 0.01 ? 4 : 2;
  return new Intl.NumberFormat(locale, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(usd);
}

/** Parse a limit field: blank or invalid reads as 0 (unlimited); negatives are refused. */
export function parseLimit(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return 0;
  const n = typeof value === 'number' ? value : Number(String(value).replace(/[\s,]/g, ''));
  if (!Number.isFinite(n) || n < 0) return null;
  return Math.floor(n);
}
