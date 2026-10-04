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
 * `src/bundleBudget.test.ts`.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';

/** Gzip bytes. Set just above the measured post-optimisation size; raise only deliberately. */
export const ROOT_LAYOUT_GZIP_BUDGET = 20 * 1024;
/** Gzip bytes of the node plus its static import closure (measured 119 KB; was 355 KB). */
export const ROOT_LAYOUT_CLOSURE_GZIP_BUDGET = 130 * 1024;

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
 */
export function findRootLayoutNode(nodesDir) {
  return readdirSync(nodesDir).find((name) => /^0\.[^.]+\.js$/.test(name)) ?? null;
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

function main() {
  const root = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist', '_app', 'immutable');
  const file = findRootLayoutNode(join(root, 'nodes'));
  if (!file) {
    console.error(`check-bundle-budget: no root layout node (0.*.js) in ${root}/nodes`);
    process.exit(1);
  }
  const kb = (n) => (n / 1024).toFixed(1);
  const own = gzipSize(readFileSync(join(root, 'nodes', file)));
  const closure = staticClosure(root, join('nodes', file)).reduce(
    (sum, f) => sum + gzipSize(readFileSync(join(root, f))),
    0
  );
  const hint =
    'Load heavy, conditionally shown components (modals, settings panels) with a dynamic import() instead of a static one.';
  let failed = false;
  if (!withinBudget(own)) {
    console.error(
      `check-bundle-budget: root layout ${file} is ${kb(own)} KB gzip, over the ${kb(
        ROOT_LAYOUT_GZIP_BUDGET
      )} KB budget. ${hint}`
    );
    failed = true;
  }
  if (!withinBudget(closure, ROOT_LAYOUT_CLOSURE_GZIP_BUDGET)) {
    console.error(
      `check-bundle-budget: root layout static closure is ${kb(closure)} KB gzip, over the ${kb(
        ROOT_LAYOUT_CLOSURE_GZIP_BUDGET
      )} KB budget. ${hint}`
    );
    failed = true;
  }
  if (failed) process.exit(1);
  console.log(
    `check-bundle-budget: OK — root layout ${kb(own)} KB gzip (budget ${kb(
      ROOT_LAYOUT_GZIP_BUDGET
    )}), ` + `static closure ${kb(closure)} KB gzip (budget ${kb(ROOT_LAYOUT_CLOSURE_GZIP_BUDGET)})`
  );
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main();
}
