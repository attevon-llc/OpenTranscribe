/**
 * Post-build gate on the size of the root layout chunk (issue #1131).
 *
 * `dist/_app/immutable/nodes/0.*.js` is the root `+layout.svelte` node. Every page load,
 * including /login, downloads it before anything renders, so anything statically imported
 * from the layout (a modal and the dozens of panels behind it, say) is paid by every
 * visitor. Heavy, conditionally shown components must be loaded with a dynamic `import()`.
 *
 * Two budgets, both gzip: the node itself, and the whole static import closure of the node
 * (the node plus every chunk it pulls in before it can run). A heavy component moved into a
 * shared chunk would dodge the first but not the second. The helpers are exported for
 * `src/bundleBudget*.test.ts`.
 *
 * A derived build (one that replaces `$lib/cloud`) may add a bounded, reasoned allowance
 * through `BUNDLE_BUDGET_ALLOWANCE_*` environment variables (issue #1255). The allowance is
 * capped here, malformed values exit 2, and an active allowance is printed on every run.
 * Headroom is printed for every metric on every run, pass or fail. See `frontend/CLAUDE.md`,
 * "Bundle budget gate".
 *
 * Issue #794 adds three checks computed from Vite's build manifest (`scripts/bundle-manifest.mjs`):
 * no locale JSON in the first-load static closure (start + app entries, root layout and error
 * nodes); a budget on the entry chunks; a budget on the first-load closure; and a budget on
 * the total JS shipped. The locale check has no budget and no allowance: a locale in first load
 * is always a defect (they are `import.meta.glob` lazy loaders, `src/lib/i18n/index.ts`).
 *
 * Issue #1327 adds two text checks, also without budget or allowance: no React runtime anywhere
 * in `_app/immutable` (`@videojs/store` installs `react` for bindings the HTML player never uses),
 * and no Video.js code in the first-load closure (the player is route-scoped). A third keeps it
 * out of the static closure of the routes that open a player only on demand (search, speakers:
 * `PLAYER_ON_DEMAND_ROUTES`) and prints each of those closures' size.
 *
 * Measured 2026-11-06 on a stock `nice -n 19 npx vite build` in `frontend/` (gzip level 9):
 *   entry chunks (entry/start + entry/app)     4,173 B
 *   first-load closure (45 chunks)           139,858 B
 *   total JS (251 files under _app/immutable) 2,566,185 B
 * Ratchet: a budget moves only in a reviewed PR that states a new measurement.
 *
 * SvelteKit 3 upgrade (issue #1326), same command on the same tree before/after, gzip level 9:
 *   static closure          135,006 B (42 files) -> 140,252 B (43 files)
 *   first-load closure      139,969 B (45 chunks) -> 144,684 B (46 chunks)
 *   entry chunks              4,167 B -> 3,649 B
 *   total JS              2,582,108 B -> 2,585,192 B
 * The ~5 KB growth is the Kit 3 client runtime (the old 25.3 KB runtime chunk is now two chunks
 * totalling ~39 KB, with the rest of the delta in svelte/vite chunking); the app's own root layout
 * node is unchanged (16,884 -> 16,909 B). It cannot be removed from app code, so the two closure
 * budgets are raised by the measured delta plus the usual ~4% headroom, and the entry-chunk
 * budget is lowered to follow the measured drop.
 *
 * Video.js 10 mini player (issue #1327 Lane D, owner decision Q1), 2026-11-07, same command on
 * the tree before/after the change, gzip level 9:
 *   total JS              2,649,972 B (261 files) -> 2,767,562 B (325 files), +117,590 B
 *   first-load closure      146,223 B -> 146,334 B; entry chunks 3,796 B -> 3,803 B
 * Of the delta, 55,642 B is the one Video.js code chunk, loaded with the search and speakers
 * routes (never first load); the other ~62 KB is 63 lazily loaded locale chunks, of which a
 * viewer fetches one. Plyr stays until Lane E removes it (about -34 KB). The total-JS budget
 * is raised to about 4% above the new measurement; the other budgets are unchanged.
 *
 * File-detail player on Video.js 10 (issue #1327 lane C, owner decision Q1), 2026-11-07, stock
 * `nice -n 19 npm run build` on the lane tree before/after, gzip level 9:
 *   total JS              2,649,976 B -> 2,774,568 B  (+124,592 B)
 *   first-load closure      146,223 B ->   146,349 B  (+126 B, Svelte's custom-element attribute
 *                                                      helper in the shared runtime chunk; no
 *                                                      Video.js code, which the gate checks)
 *   entry chunks              3,795 B ->     3,817 B
 * The delta is the route-scoped composed controls plus the library's lazy locale packs (53
 * chunks, about 62 KB in total, of which a viewer downloads one, about 1.3 KB). Plyr is still
 * shipped for the mini player until lane E removes it (about 34 KB back). The total-JS budget is
 * raised by the delta plus ~4% of it (124,592 + 4,984 B), rounded up to whole KiB.
 *
 * Plyr removed (issue #1327 lane E), 2026-11-07, stock `nice -n 19 npm run build` on the lane tree
 * before/after, gzip level 9:
 *   total JS              2,747,197 B -> 2,747,199 B  (no change: no module imported `plyr` any
 *                                                      more after lane D, so the package was
 *                                                      already tree-shaken out of the bundle)
 *   first-load closure      146,291 B ->   146,293 B; entry chunks 3,772 B -> 3,772 B
 * The planned -34 KB therefore never appears here; removing the dependency only shrinks
 * `node_modules` and the licence inventory. The total-JS budget is lowered from 2,811 KiB to
 * 2,791 KiB (about 4% above the measurement, whole KiB); no other budget changes.
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join, normalize, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';
import {
  PLAYER_ON_DEMAND_ROUTES,
  REACT_RUNTIME_MARKER,
  VIDEOJS_MARKER,
  filesContaining,
  firstLoadRoots,
  localeOffenders,
  localeSentinels,
  manifestClosure,
  routeNodeKey,
} from './bundle-manifest.mjs';

/**
 * Gzip bytes of the root layout node. Headroom policy (issue #1255): keep about 10% above the
 * measured size so an ordinary feature or a derived overlay does not turn the gate red; measured
 * 20,068 B on the stock build after PR #1256. Raise only in a reviewed PR that states the new
 * measurement, never to absorb a regression.
 */
export const ROOT_LAYOUT_GZIP_BUDGET = 22 * 1024;
/**
 * Gzip bytes of the node plus its static import closure. Same policy: about 4% above the
 * measured 130,981 B (the closure is large, so a smaller fraction is the same absolute room).
 */
export const ROOT_LAYOUT_CLOSURE_GZIP_BUDGET = 143 * 1024;

/** Gzip bytes of the entry chunks (start + app); measured 3,649 B on Kit 3, about 19% headroom (tiny chunk, so a larger fraction is still small). */
export const ENTRY_CHUNKS_GZIP_BUDGET = 4352;
/** Gzip bytes of the manifest first-load closure; measured 144,684 B on Kit 3, about 4% headroom. */
export const FIRST_LOAD_GZIP_BUDGET = 147 * 1024;
/**
 * Gzip bytes of all JS under `_app/immutable`; measured 2,566,185 B, about 3% headroom.
 * Re-measured 2026-11-07 (stock `nice -n 19 npx vite build`, gzip 9): 2,641,830 B on the tree
 * before the comment composer, i.e. 90 B of headroom left, and 2,649,803 B with it. 7,733 B of
 * that 7,973 B delta is translated copy in the 12 lazily loaded locale chunks (a user downloads
 * one, about 650 B), so it is content, not a regression. Reset to about 3% above the new measure.
 * Raised for the Video.js mini player (#1327, owner decision Q1): measured 2,767,562 B, budget
 * about 4% above it (see the header).
 * With both players merged (they share the lazy locale chunks) it measured 2,747,199 B on
 * 2026-11-07, 4.6% under this budget, so it is kept.
 */
export const TOTAL_JS_GZIP_BUDGET = 2791 * 1024;

/** The most a derived build may add to the node budget. Raising it is a core PR. */
export const MAX_ALLOWANCE_NODE_BYTES = 3072;
/** The most a derived build may add to the closure budget. Raising it is a core PR. */
export const MAX_ALLOWANCE_CLOSURE_BYTES = 20480;
/** Headroom under this is flagged `LOW` in the report; it never fails a build. */
export const LOW_HEADROOM_BYTES = 512;

// Build-time only. Never start these with `VITE_`: Vite inlines `VITE_*` into the client
// bundle, which would ship the reason text to every browser and change the bytes measured.
export const ENV_ALLOWANCE_NODE = 'BUNDLE_BUDGET_ALLOWANCE_NODE_BYTES';
export const ENV_ALLOWANCE_CLOSURE = 'BUNDLE_BUDGET_ALLOWANCE_CLOSURE_BYTES';
export const ENV_ALLOWANCE_REASON = 'BUNDLE_BUDGET_ALLOWANCE_REASON';
export const ENV_PREFIX = 'BUNDLE_BUDGET_';
const ACCEPTED_ENV = [ENV_ALLOWANCE_NODE, ENV_ALLOWANCE_CLOSURE, ENV_ALLOWANCE_REASON];

const REASON_MIN = 20;
const REASON_MAX = 300;
const HINT =
  'Load heavy, conditionally shown components (modals, settings panels) with a dynamic import() instead of a static one.';

/** A malformed allowance configuration. The gate maps it to exit code 2 (misuse). */
export class BudgetConfigError extends Error {
  constructor(message) {
    super(message);
    this.name = 'BudgetConfigError';
  }
}

/**
 * Static (non-dynamic) relative imports of a built chunk.
 * @param {string} source
 * @returns {string[]}
 */
export function staticImports(source) {
  return [...source.matchAll(/(?:\bfrom|\bimport)\s*"(\.{1,2}\/[^"]+\.js)"/g)].map((m) => m[1]);
}

/**
 * Every file the node needs before it can run, itself included (paths relative to *root*).
 * @param {string} root immutable dir
 * @param {string} entry e.g. `nodes/0.abc.js`
 * @returns {string[]}
 */
export function staticClosure(root, entry) {
  const seen = new Set();
  const stack = [entry];
  while (stack.length > 0) {
    const file = /** @type {string} */ (stack.pop());
    if (seen.has(file)) continue;
    seen.add(file);
    const source = readFileSync(join(root, file), 'utf8');
    for (const rel of staticImports(source)) stack.push(normalize(join(dirname(file), rel)));
  }
  return [...seen];
}

/**
 * @param {string} nodesDir
 * @returns {string | null} file name of the root layout node, or null if absent
 * @throws {Error} when more than one `0.*.js` exists (a stale hash left in a reused dist
 *   would otherwise be measured arbitrarily)
 */
export function findRootLayoutNode(nodesDir) {
  let names;
  try {
    names = readdirSync(nodesDir);
  } catch (err) {
    if (err && err.code === 'ENOENT') return null;
    throw err;
  }
  const matches = names.filter((name) => /^0\.[^.]+\.js$/.test(name)).sort();
  if (matches.length > 1) {
    throw new Error(
      `more than one root layout node in ${nodesDir}: ${matches.join(', ')} (stale build output?)`
    );
  }
  return matches[0] ?? null;
}

/**
 * @param {Buffer | string} source
 * @returns {number}
 */
export function gzipSize(source) {
  return gzipSync(source, { level: 9 }).length;
}

/**
 * @param {number} size
 * @param {number} [budget]
 * @returns {boolean}
 */
export function withinBudget(size, budget = ROOT_LAYOUT_GZIP_BUDGET) {
  return size <= budget;
}

/**
 * @param {string | undefined} raw
 * @param {string} name
 * @param {number} cap
 * @returns {number} 0 for unset or blank
 */
function parseBytes(raw, name, cap) {
  const value = (raw ?? '').trim();
  if (value === '') return 0; // `ARG X=` then `ENV X=${X}` yields '', and a stock build stays strict
  if (!/^[0-9]{1,7}$/.test(value)) {
    throw new BudgetConfigError(
      `${name}=${JSON.stringify(value)} must be a whole number of bytes (digits only, at most 7)`
    );
  }
  const bytes = Number(value);
  if (bytes > cap) {
    throw new BudgetConfigError(`${name}=${bytes} is over the cap of ${cap} bytes set in core`);
  }
  return bytes;
}

/** True when *text* holds a control character (a newline could forge a log line). */
function hasControlChars(text) {
  for (const ch of text) {
    const code = ch.codePointAt(0);
    if (code < 0x20 || (code >= 0x7f && code <= 0x9f) || code === 0x2028 || code === 0x2029) {
      return true;
    }
  }
  return false;
}

/**
 * Read and validate the allowance from the build environment.
 * @param {Record<string, string | undefined>} env
 * @returns {{ nodeBytes: number, closureBytes: number, reason: string | null }}
 * @throws {BudgetConfigError}
 */
export function readAllowance(env) {
  for (const key of Object.keys(env)) {
    if (key.toUpperCase().startsWith(ENV_PREFIX) && !ACCEPTED_ENV.includes(key)) {
      throw new BudgetConfigError(`unknown variable ${key}; accepted: ${ACCEPTED_ENV.join(', ')}`);
    }
  }
  const nodeBytes = parseBytes(
    env[ENV_ALLOWANCE_NODE],
    ENV_ALLOWANCE_NODE,
    MAX_ALLOWANCE_NODE_BYTES
  );
  const closureBytes = parseBytes(
    env[ENV_ALLOWANCE_CLOSURE],
    ENV_ALLOWANCE_CLOSURE,
    MAX_ALLOWANCE_CLOSURE_BYTES
  );
  if (nodeBytes === 0 && closureBytes === 0) return { nodeBytes, closureBytes, reason: null };
  const reason = (env[ENV_ALLOWANCE_REASON] ?? '').trim();
  if (reason.length < REASON_MIN || reason.length > REASON_MAX) {
    throw new BudgetConfigError(
      `${ENV_ALLOWANCE_REASON} is required with an allowance and must be ${REASON_MIN} to ${REASON_MAX} characters (got ${reason.length})`
    );
  }
  if (hasControlChars(reason)) {
    throw new BudgetConfigError(`${ENV_ALLOWANCE_REASON} must be a single line of plain text`);
  }
  return { nodeBytes, closureBytes, reason };
}

/**
 * Base budget plus allowance, per metric. A node allowance never rescues the closure.
 * @param {{ nodeBytes: number, closureBytes: number }} allowance
 * @param {{ node: number, closure: number }} [base]
 * @returns {{ node: number, closure: number }}
 */
export function effectiveBudgets(
  allowance,
  base = { node: ROOT_LAYOUT_GZIP_BUDGET, closure: ROOT_LAYOUT_CLOSURE_GZIP_BUDGET }
) {
  return { node: base.node + allowance.nodeBytes, closure: base.closure + allowance.closureBytes };
}

/**
 * @param {number} size gzip bytes
 * @param {number} budget gzip bytes
 * @returns {{ size: number, budget: number, headroom: number, over: boolean, low: boolean }}
 */
export function evaluate(size, budget) {
  const headroom = budget - size;
  return {
    size,
    budget,
    headroom,
    over: !withinBudget(size, budget),
    low: headroom >= 0 && headroom < LOW_HEADROOM_BYTES,
  };
}

function metricLine(label, result, base, allowance) {
  const parts = `base ${base} + allowance ${allowance}`;
  const head = `check-bundle-budget: ${label}  ${result.size} B gzip  budget ${result.budget} B (${parts})`;
  if (result.over) {
    const used = allowance > 0 ? `  allowance exhausted (all ${allowance} B used)` : '';
    return `${head}  OVER BY ${-result.headroom} B${used}`;
  }
  const pct = ((result.headroom / result.budget) * 100).toFixed(1);
  const used =
    allowance > 0 ? `  allowance used ${Math.max(0, result.size - base)} of ${allowance} B` : '';
  return `${head}  headroom ${result.headroom} B (${pct}%)${result.low ? '  LOW' : ''}${used}`;
}

/**
 * Run the gate against a built `_app/immutable` directory. Pure apart from reading *root*.
 * @param {{ root: string, env?: Record<string, string | undefined>,
 *   base?: { node: number, closure: number }, manifestPath?: string | null,
 *   localeDir?: string | null, sourceRoot?: string | null, playerRoutes?: string[],
 *   extra?: { entry: number, firstLoad: number, totalJs: number } }} options
 * @returns {{ code: number, stdout: string[], stderr: string[] }}
 */
export function run({
  root,
  env = process.env,
  base = { node: ROOT_LAYOUT_GZIP_BUDGET, closure: ROOT_LAYOUT_CLOSURE_GZIP_BUDGET },
  manifestPath = null,
  localeDir = null,
  sourceRoot = null,
  playerRoutes = PLAYER_ON_DEMAND_ROUTES,
  extra = {
    entry: ENTRY_CHUNKS_GZIP_BUDGET,
    firstLoad: FIRST_LOAD_GZIP_BUDGET,
    totalJs: TOTAL_JS_GZIP_BUDGET,
  },
}) {
  const out = [];
  const err = [];
  let allowance;
  try {
    allowance = readAllowance(env);
  } catch (e) {
    if (!(e instanceof BudgetConfigError)) throw e;
    return {
      code: 2,
      stdout: out,
      stderr: [`check-bundle-budget: configuration error: ${e.message}`],
    };
  }
  let file;
  try {
    file = findRootLayoutNode(join(root, 'nodes'));
  } catch (e) {
    return { code: 1, stdout: out, stderr: [`check-bundle-budget: ${e.message}`] };
  }
  if (!file) {
    return {
      code: 1,
      stdout: out,
      stderr: [`check-bundle-budget: no root layout node (0.*.js) in ${root}/nodes`],
    };
  }
  const own = gzipSize(readFileSync(join(root, 'nodes', file)));
  const files = staticClosure(root, join('nodes', file));
  const closure = files.reduce((sum, f) => sum + gzipSize(readFileSync(join(root, f))), 0);
  const budgets = effectiveBudgets(allowance, base);
  const node = evaluate(own, budgets.node);
  const total = evaluate(closure, budgets.closure);

  out.push(metricLine(`root layout ${file}`, node, base.node, allowance.nodeBytes));
  out.push(
    metricLine(
      `static closure (${files.length} files)`,
      total,
      base.closure,
      allowance.closureBytes
    )
  );
  if (allowance.reason) {
    out.push(
      `check-bundle-budget: ALLOWANCE ACTIVE node +${allowance.nodeBytes} B, closure +${allowance.closureBytes} B. Reason: ${allowance.reason}`
    );
    if (allowance.nodeBytes > allowance.closureBytes) {
      out.push(
        'check-bundle-budget: WARNING the node allowance exceeds the closure allowance; the closure includes the node, so it cannot be fully used'
      );
    }
    if (allowance.nodeBytes > 0 && own <= base.node) {
      out.push(
        `check-bundle-budget: WARNING the node allowance is not needed (${own} B is within the base ${base.node} B); remove it`
      );
    }
    if (allowance.closureBytes > 0 && closure <= base.closure) {
      out.push(
        `check-bundle-budget: WARNING the closure allowance is not needed (${closure} B is within the base ${base.closure} B); remove it`
      );
    }
  } else if ((env[ENV_ALLOWANCE_REASON] ?? '').trim() !== '') {
    out.push(
      `check-bundle-budget: WARNING ${ENV_ALLOWANCE_REASON} is set but both allowances are 0; it is ignored`
    );
  }
  const manifestResult = manifestChecks({
    root,
    manifestPath,
    localeDir,
    sourceRoot,
    playerRoutes,
    extra,
    allowance,
  });
  out.push(...manifestResult.lines);
  err.push(...manifestResult.errors);
  if (manifestResult.errors.length > 0 && !(node.over || total.over)) {
    return { code: 1, stdout: out, stderr: err };
  }
  if (node.over || total.over) {
    const which = [node.over && 'root layout', total.over && 'static closure'].filter(Boolean);
    err.push(`check-bundle-budget: FAILED, over budget: ${which.join(', ')}. ${HINT}`);
    return { code: 1, stdout: out, stderr: err };
  }
  if (manifestResult.errors.length > 0) return { code: 1, stdout: out, stderr: err };
  out.push('check-bundle-budget: OK');
  return { code: 0, stdout: out, stderr: err };
}

/** Every `.js` under *dir*, recursively (sizes the total-JS budget). */
function jsFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((d) =>
    d.isDirectory() ? jsFiles(join(dir, d.name)) : d.name.endsWith('.js') ? [join(dir, d.name)] : []
  );
}

/**
 * Manifest-based checks (issue #794). Fails closed: a missing manifest is an error, so a
 * Vite/SvelteKit upgrade that moves it cannot silently disable the gate.
 * @returns {{ lines: string[], errors: string[] }}
 */
function manifestChecks({
  root,
  manifestPath,
  localeDir,
  sourceRoot,
  playerRoutes,
  extra,
  allowance,
}) {
  if (!manifestPath) return { lines: [], errors: [] };
  if (!existsSync(manifestPath)) {
    return {
      lines: [],
      errors: [`check-bundle-budget: build manifest not found at ${manifestPath}`],
    };
  }
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  const dist = join(root, '..', '..');
  const readChunk = (file) => readFileSync(join(dist, file), 'utf8');
  const { entries, roots } = firstLoadRoots(manifest);
  if (entries.length !== 2 || roots.length < 3) {
    return {
      lines: [],
      errors: [
        `check-bundle-budget: could not find the SvelteKit entry chunks in the manifest (${roots.length} roots)`,
      ],
    };
  }
  const closure = manifestClosure(manifest, roots);
  const size = (keys) =>
    [...new Set(keys.map((k) => manifest[k].file))].reduce(
      (sum, f) => sum + gzipSize(readFileSync(join(dist, f))),
      0
    );
  const entry = evaluate(size(entries), extra.entry);
  const first = evaluate(size(closure), extra.firstLoad + allowance.closureBytes);
  const allJs = jsFiles(root);
  const totalSize = allJs.reduce((sum, f) => sum + gzipSize(readFileSync(f)), 0);
  const total = evaluate(totalSize, extra.totalJs + allowance.closureBytes);
  const lines = [
    metricLine('entry chunks', entry, extra.entry, 0),
    metricLine(
      `first-load closure (${closure.length} chunks)`,
      first,
      extra.firstLoad,
      allowance.closureBytes
    ),
    metricLine('total JS', total, extra.totalJs, allowance.closureBytes),
  ];
  const errors = [];
  const over = [
    entry.over && 'entry chunks',
    first.over && 'first-load closure',
    total.over && 'total JS',
  ].filter(Boolean);
  if (over.length > 0)
    errors.push(`check-bundle-budget: FAILED, over budget: ${over.join(', ')}. ${HINT}`);
  const react = filesContaining(allJs, (f) => readFileSync(f, 'utf8'), REACT_RUNTIME_MARKER);
  if (react.length > 0) {
    const rel = react.map((f) => relative(dist, f)).sort();
    errors.push(
      `check-bundle-budget: FAILED, React runtime in the client bundle: ${rel.join(
        ', '
      )}. Import Video.js from @videojs/html only, never a React entry point (issue #1327).`
    );
  } else {
    lines.push('check-bundle-budget: no React runtime in the client bundle');
  }
  const firstLoadFiles = [...new Set(closure.map((k) => manifest[k].file))];
  const vjs = filesContaining(firstLoadFiles, readChunk, VIDEOJS_MARKER);
  if (vjs.length > 0) {
    errors.push(
      `check-bundle-budget: FAILED, Video.js code in the first-load closure: ${vjs
        .sort()
        .join(', ')}. The player is route-scoped; load it with a dynamic import() (issue #1327).`
    );
  } else {
    lines.push('check-bundle-budget: no Video.js code in the first-load closure');
  }
  if (sourceRoot) {
    const readSource = (key) => {
      try {
        return readFileSync(join(sourceRoot, key), 'utf8');
      } catch {
        return '';
      }
    };
    for (const route of playerRoutes) {
      const key = routeNodeKey(manifest, readSource, route);
      if (!key) {
        errors.push(`check-bundle-budget: could not find the build node for ${route}`);
        continue;
      }
      const routeClosure = manifestClosure(manifest, [key]);
      const routeFiles = [...new Set(routeClosure.map((k) => manifest[k].file))];
      const routeVjs = filesContaining(routeFiles, readChunk, VIDEOJS_MARKER);
      const label = `${route} static closure (${routeFiles.length} chunks)  ${size(
        routeClosure
      )} B gzip`;
      if (routeVjs.length > 0) {
        errors.push(
          `check-bundle-budget: FAILED, Video.js code in the ${label}: ${routeVjs
            .sort()
            .join(
              ', '
            )}. The route opens its player on demand; load it with import() (issue #1327).`
        );
      } else {
        lines.push(`check-bundle-budget: ${label}, no Video.js code`);
      }
    }
  }
  if (localeDir) {
    const bad = localeOffenders(manifest, closure, readChunk, localeSentinels(localeDir));
    if (bad.length > 0) {
      errors.push(
        `check-bundle-budget: FAILED, locale JSON in the first-load closure: ${bad.join(
          '; '
        )}. Locales must load through the lazy import.meta.glob loaders in src/lib/i18n/index.ts.`
      );
    } else {
      lines.push('check-bundle-budget: no locale JSON in the first-load closure');
    }
  }
  return { lines, errors };
}

function main() {
  const frontend = join(dirname(fileURLToPath(import.meta.url)), '..');
  const root = join(frontend, 'dist', '_app', 'immutable');
  const { code, stdout, stderr } = run({
    root,
    manifestPath: join(frontend, '.svelte-kit', 'output', 'client', '.vite', 'manifest.json'),
    localeDir: join(frontend, 'src', 'lib', 'i18n', 'locales'),
    sourceRoot: frontend,
  });
  for (const line of stdout) console.log(line);
  for (const line of stderr) console.error(line);
  process.exit(code);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main();
}
