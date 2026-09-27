/**
 * Issue #1028 — connect-src must not carry bare `ws:`/`wss:` sources.
 *
 * A bare scheme source allows a socket to ANY host (and `ws:` a plaintext one), so
 * injected or supply-chain script could stream transcript content anywhere. `'self'`
 * already matches same-host ws:/wss: under CSP Level 3, which is all
 * `stores/websocket.ts` needs — it builds its URL from `window.location`.
 *
 * The built meta itself is gated by `scripts/check-built-csp.mjs` (`postbuild`); this
 * file pins the config that produces it and the checker's own parsing.
 */
import { describe, expect, it } from 'vitest';
import config from '../svelte.config.js';
import { bareSchemeSources, extractMetaCsp, parseCsp } from '../scripts/check-built-csp.mjs';

const directives = config.kit?.csp?.directives as Record<string, string[]>;

describe('kit.csp directives (svelte.config.js)', () => {
  it('connect-src is exactly self', () => {
    expect(directives['connect-src']).toEqual(['self']);
  });

  it('no directive carries a bare network scheme source', () => {
    expect(bareSchemeSources(directives)).toEqual([]);
  });

  it('keeps the non-network scheme sources the app relies on', () => {
    expect(directives['img-src']).toEqual(['self', 'data:', 'blob:']);
    expect(directives['worker-src']).toEqual(['self', 'blob:']);
  });
});

describe('check-built-csp parsing', () => {
  const html = (policy: string) =>
    `<!doctype html><html><head><meta charset="utf-8">` +
    `<meta http-equiv="content-security-policy" content="${policy}"></head></html>`;

  it('extracts the meta policy SvelteKit emits', () => {
    expect(extractMetaCsp(html("default-src 'self'; connect-src 'self'"))).toBe(
      "default-src 'self'; connect-src 'self'"
    );
  });

  it('returns null when the page has no CSP meta', () => {
    expect(extractMetaCsp('<html><head><meta charset="utf-8"></head></html>')).toBeNull();
  });

  it('flags the pre-fix connect-src', () => {
    const parsed = parseCsp("default-src 'self'; connect-src 'self' ws: wss:");
    expect(parsed['connect-src']).toEqual(["'self'", 'ws:', 'wss:']);
    expect(bareSchemeSources(parsed)).toEqual(['connect-src ws:', 'connect-src wss:']);
  });

  it('flags a bare http(s) source in any directive, case-insensitively', () => {
    expect(bareSchemeSources(parseCsp("img-src 'self' HTTPS:; script-src http:"))).toEqual([
      'img-src HTTPS:',
      'script-src http:',
    ]);
  });

  it('accepts self, data:/blob: and an explicit scheme+host', () => {
    const policy = "connect-src 'self' wss://app.example.com; img-src 'self' data: blob:";
    expect(bareSchemeSources(parseCsp(policy))).toEqual([]);
  });
});
