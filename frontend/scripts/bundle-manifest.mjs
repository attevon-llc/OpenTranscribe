/**
 * First-load analysis of a SvelteKit client build from Vite's build manifest.
 *
 * Vite documents `build.manifest` (`.vite/manifest.json`): each entry has `file`, `imports`
 * (static chunk keys, loaded eagerly) and `dynamicImports` (loaded on demand). The static
 * `imports` graph from the entry chunks is therefore exactly what a first page load must
 * download. https://vite.dev/guide/backend-integration.html and
 * https://vite.dev/config/build-options.html#build-manifest
 *
 * SvelteKit forces `build.manifest: true` and leaves the file at
 * `.svelte-kit/output/client/.vite/manifest.json`. Used by `check-bundle-budget.mjs`.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

/** Manifest keys SvelteKit's SPA boots from: runtime start, app, root layout, error page. */
const ENTRY_KEY = /(?:runtime\/client\/entry\.js|client-optimized\/app\.js)$/;
const NODE_KEY = /client-optimized\/nodes\/[01]\.js$/;
const ANY_NODE_KEY = /client-optimized\/nodes\/\d+\.js$/;
const LOCALE_SRC = /(?:^|\/)i18n\/locales\/[^/]+\.json$/;

/**
 * @param {Record<string, any>} manifest parsed `manifest.json`
 * @returns {{ entries: string[], roots: string[] }} `entries` are the start and app chunks
 *   (the "entry chunk" budget); `roots` adds the root layout and error nodes.
 */
export function firstLoadRoots(manifest) {
  const keys = Object.keys(manifest);
  const entries = keys.filter((k) => ENTRY_KEY.test(k));
  const nodes = keys.filter((k) => NODE_KEY.test(k));
  return { entries, roots: [...entries, ...nodes] };
}

/**
 * Static (`imports`) closure of *roots* over the manifest; dynamic imports are not followed.
 * @param {Record<string, any>} manifest
 * @param {string[]} roots
 * @returns {string[]} manifest keys
 */
export function manifestClosure(manifest, roots) {
  const seen = new Set();
  const stack = [...roots];
  while (stack.length > 0) {
    const key = /** @type {string} */ (stack.pop());
    if (seen.has(key)) continue;
    if (!manifest[key]) throw new Error(`manifest has no entry for ${key}`);
    seen.add(key);
    stack.push(...(manifest[key].imports ?? []));
  }
  return [...seen];
}

/**
 * Long string values of the locale JSON files. A locale statically imported into the
 * first-load graph is usually inlined into a chunk that has no `src` of its own, so the
 * manifest alone cannot see it; a distinctive string still shows up in the chunk text.
 * @param {string} localeDir
 * @returns {string[]}
 */
export function localeSentinels(localeDir) {
  const out = [];
  for (const name of readdirSync(localeDir)
    .filter((n) => n.endsWith('.json'))
    .sort()) {
    const strings = [];
    (function walk(v) {
      if (typeof v === 'string') strings.push(v);
      else if (v && typeof v === 'object') Object.values(v).forEach(walk);
    })(JSON.parse(readFileSync(join(localeDir, name), 'utf8')));
    // Plain ASCII only: JSON escaping in a minified bundle would not match byte for byte.
    const best = strings
      .filter((s) => /^[ -~]+$/.test(s) && !/["'\\`$]/.test(s))
      .sort((a, b) => b.length - a.length)[0];
    if (best && best.length >= 24) out.push(best);
  }
  return out;
}

/**
 * Locale JSON found in the first-load closure.
 * @param {Record<string, any>} manifest
 * @param {string[]} closure manifest keys
 * @param {(file: string) => string} readChunk chunk text by manifest `file`
 * @param {string[]} sentinels from {@link localeSentinels}
 * @returns {string[]} human-readable offenders, empty when clean
 */
export function localeOffenders(manifest, closure, readChunk, sentinels) {
  const bad = [];
  for (const key of closure) {
    const entry = manifest[key];
    if (LOCALE_SRC.test(key) || LOCALE_SRC.test(entry.src ?? '')) {
      bad.push(`${entry.src ?? key} (own chunk ${entry.file})`);
      continue;
    }
    const text = readChunk(entry.file);
    if (sentinels.some((s) => text.includes(s))) bad.push(`locale text inlined in ${entry.file}`);
  }
  return bad;
}

/**
 * Text every React runtime build carries: its element and memo-cache symbols are created with
 * `Symbol.for("react.…")`, a registry key that minifiers cannot rename. `@videojs/store` lists
 * `react` and `react-compiler-runtime` as dependencies for its React bindings (issue #1327);
 * the HTML player never imports them, and this keeps it that way.
 */
export const REACT_RUNTIME_MARKER = 'Symbol.for("react.';

/**
 * Text Video.js 10 modules carry (`Symbol("@videojs/…")` state keys, `"@videojs/…"` ids). The
 * player is route-scoped, so any of it in a first-load chunk means a static import leaked.
 */
export const VIDEOJS_MARKER = '@videojs/';

/**
 * Files whose text contains *marker*.
 * @param {string[]} files
 * @param {(file: string) => string} read
 * @param {string} marker
 * @returns {string[]}
 */
export function filesContaining(files, read, marker) {
  return files.filter((file) => read(file).includes(marker));
}

/**
 * Routes that show a media player only on demand (issue #1327): their own static closure must
 * not carry Video.js, so opening the page does not download a player nobody has asked for.
 */
export const PLAYER_ON_DEMAND_ROUTES = [
  'src/routes/search/+page.svelte',
  'src/routes/speakers/+page.svelte',
];

/**
 * Manifest key of the SvelteKit node that renders *routeFile*. The manifest names nodes by
 * index only; the generated node module (the key, read relative to the project root) is what
 * imports the route component, so it is matched by that import.
 * @param {Record<string, any>} manifest
 * @param {(key: string) => string} readSource text of a key's source module ('' if absent)
 * @param {string} routeFile e.g. `src/routes/search/+page.svelte`
 * @returns {string | null} the one matching key, or null when none or several match
 */
export function routeNodeKey(manifest, readSource, routeFile) {
  const hits = Object.keys(manifest).filter(
    (k) => ANY_NODE_KEY.test(k) && readSource(k).includes(`/${routeFile}"`)
  );
  return hits.length === 1 ? hits[0] : null;
}
