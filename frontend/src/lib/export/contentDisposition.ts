/**
 * Read the file name the SERVER chose for a download out of its `Content-Disposition`
 * header (RFC 6266), so the SPA saves the file under exactly that name instead of
 * recomputing it (issue #1283).
 *
 * The extension used to be rebuilt here as `${filename}.${format}`, which is a second copy of
 * a naming rule the server already owns, and which drifts the moment a format's extension is
 * not its token. The server sends both `filename*=UTF-8''<percent-encoded>` (exact, any
 * script) and an ASCII `filename="..."` fallback; the exact form wins.
 */

/** The decoded file name, or `null` when the header is missing or carries none. */
export function parseContentDispositionFilename(header: unknown): string | null {
  if (typeof header !== 'string' || header.length === 0) return null;

  const extended = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
  if (extended) {
    try {
      const decoded = decodeURIComponent(extended[1].trim());
      if (decoded) return decoded;
    } catch {
      // A malformed escape: fall through to the ASCII form rather than lose the name.
    }
  }

  const plain = /filename\s*=\s*"([^"]*)"/i.exec(header) ?? /filename\s*=\s*([^;]+)/i.exec(header);
  const value = plain?.[1]?.trim();
  return value ? value : null;
}
