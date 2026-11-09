// Matchers in priority order: Edge and Opera also carry "Chrome/" and "Safari/", and Chrome
// carries "Safari/", so the most specific token must be tried first.
const BROWSERS: ReadonlyArray<readonly [RegExp, string]> = [
  [/Edg\//, 'Edge'],
  [/OPR\/|Opera/, 'Opera'],
  [/Chrome\//, 'Chrome'],
  [/Firefox\//, 'Firefox'],
  [/Safari\//, 'Safari'],
];

// Android UAs say "Linux" and iOS UAs say "Mac OS X", so those come before their generic OS.
const PLATFORMS: ReadonlyArray<readonly [RegExp, string]> = [
  [/Windows/, 'Windows'],
  [/Android/, 'Android'],
  [/iPhone|iPad|iOS/, 'iOS'],
  [/Mac OS X|Macintosh/, 'macOS'],
  [/Linux/, 'Linux'],
];

function firstMatch(table: ReadonlyArray<readonly [RegExp, string]>, ua: string): string | null {
  return table.find(([pattern]) => pattern.test(ua))?.[1] ?? null;
}

/**
 * Coarse "Browser — OS" label for a user-agent string, or null when `ua` is empty. Deliberately
 * shallow — a hint about "which device is this", not UA sniffing — so an unrecognised agent
 * falls back to the raw string rather than claiming something wrong.
 */
export function describeUserAgent(ua: string | null): string | null {
  if (!ua) return null;
  const browser = firstMatch(BROWSERS, ua);
  const platform = firstMatch(PLATFORMS, ua);
  if (browser && platform) return `${browser} — ${platform}`;
  return browser ?? platform ?? ua;
}
