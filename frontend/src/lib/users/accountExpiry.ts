/**
 * The account-expiration editor's date conversions (FedRAMP AC-2 time-boxed accounts).
 *
 * The editor is an `<input type="date">` (`YYYY-MM-DD`); the server stores
 * `account_expires_at` as a timestamp. An account stays usable through the whole
 * chosen day, so a date is sent as that day's last second.
 */

/** The `<input type="date">` value for a stored expiry, or `''` when there is none. */
export function toDateInputValue(expiresAt: string | null | undefined): string {
  return expiresAt ? expiresAt.slice(0, 10) : '';
}

/** The `account_expires_at` to send for a date-input value; `null` clears the expiry. */
export function toEndOfDayTimestamp(dateValue: string): string | null {
  return dateValue ? `${dateValue}T23:59:59` : null;
}
