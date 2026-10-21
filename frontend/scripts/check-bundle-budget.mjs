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
 * Headroom is printed for both metrics on every run, pass or fail. See `frontend/CLAUDE.md`,
 * "Bundle budget gate".
 */
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';

/** Gzip bytes. Set just above the measured post-optimisation size; raise only deliberately. */
export const ROOT_LAYOUT_GZIP_BUDGET = 20 * 1024;
/** Gzip bytes of the node plus its static import closure; raise only deliberately. */
export const ROOT_LAYOUT_CLOSURE_GZIP_BUDGET = 130 * 1024;

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
 *   base?: { node: number, closure: number } }} options
 * @returns {{ code: number, stdout: string[], stderr: string[] }}
 */
export function run({
  root,
  env = process.env,
  base = { node: ROOT_LAYOUT_GZIP_BUDGET, closure: ROOT_LAYOUT_CLOSURE_GZIP_BUDGET },
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
  if (node.over || total.over) {
    const which = [node.over && 'root layout', total.over && 'static closure'].filter(Boolean);
    err.push(`check-bundle-budget: FAILED, over budget: ${which.join(', ')}. ${HINT}`);
    return { code: 1, stdout: out, stderr: err };
  }
  out.push('check-bundle-budget: OK');
  return { code: 0, stdout: out, stderr: err };
}

function main() {
  const root = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist', '_app', 'immutable');
  const { code, stdout, stderr } = run({ root });
  for (const line of stdout) console.log(line);
  for (const line of stderr) console.error(line);
  process.exit(code);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main();
}
