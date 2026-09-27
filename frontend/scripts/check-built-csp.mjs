/**
 * Post-build gate on the CSP SvelteKit emits into `dist/index.html` (issue #1028).
 *
 * `kit.csp` in svelte.config.js is rendered into a `<meta http-equiv>` at build time, so
 * the built file — not the config — is what browsers enforce. This fails the build when
 * that meta is missing or when any directive carries a bare network scheme source
 * (`ws:`, `wss:`, `http:`, `https:`): a scheme-wide source allows a request or socket to
 * ANY host, which is the exfiltration path a CSP exists to close. `'self'` already covers
 * same-host ws:/wss: in CSP Level 3, which is all the notifications socket needs.
 *
 * Runs as `postbuild`, so `npm run build` (the push-stage hook, CI, Dockerfile.prod) all
 * enforce it. The parsing helpers are exported for `src/csp.test.ts`.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const BARE_NETWORK_SCHEMES = ['ws:', 'wss:', 'http:', 'https:'];

/**
 * The `content` of the CSP `<meta http-equiv>` in *html*, or null if there is none.
 * @param {string} html
 * @returns {string | null}
 */
export function extractMetaCsp(html) {
  for (const tag of html.match(/<meta\b[^>]*>/gi) ?? []) {
    if (!/http-equiv\s*=\s*["']content-security-policy["']/i.test(tag)) continue;
    const content = tag.match(/content\s*=\s*"([^"]*)"/i) ?? tag.match(/content\s*=\s*'([^']*)'/i);
    return content ? content[1] : null;
  }
  return null;
}

/**
 * Parse a policy string into `{ directive: [sources] }`.
 * @param {string} policy
 * @returns {Record<string, string[]>}
 */
export function parseCsp(policy) {
  /** @type {Record<string, string[]>} */
  const directives = {};
  for (const part of policy.split(';')) {
    const [name, ...sources] = part.trim().split(/\s+/).filter(Boolean);
    if (name) directives[name.toLowerCase()] = sources;
  }
  return directives;
}

/**
 * Every `directive source` pair whose source is a bare network scheme.
 * @param {Record<string, string[]>} directives
 * @returns {string[]}
 */
export function bareSchemeSources(directives) {
  /** @type {string[]} */
  const found = [];
  for (const [name, sources] of Object.entries(directives)) {
    for (const source of sources) {
      if (BARE_NETWORK_SCHEMES.includes(source.toLowerCase())) found.push(`${name} ${source}`);
    }
  }
  return found;
}

function main() {
  const indexHtml = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist', 'index.html');
  const policy = extractMetaCsp(readFileSync(indexHtml, 'utf8'));
  if (policy === null) {
    console.error(`check-built-csp: no CSP <meta http-equiv> in ${indexHtml}`);
    process.exit(1);
  }
  const directives = parseCsp(policy);
  if (!directives['connect-src']) {
    console.error('check-built-csp: the built CSP has no connect-src directive');
    process.exit(1);
  }
  const offenders = bareSchemeSources(directives);
  if (offenders.length > 0) {
    console.error(
      `check-built-csp: bare scheme source(s) allow any host: ${offenders.join(', ')}. ` +
        "Use 'self' (it covers same-host ws:/wss:) or an explicit scheme+host."
    );
    process.exit(1);
  }
  console.log(`check-built-csp: OK — connect-src ${directives['connect-src'].join(' ')}`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main();
}
