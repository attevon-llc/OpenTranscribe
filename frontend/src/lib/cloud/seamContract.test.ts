/**
 * Issue #1255 — the `$lib/cloud` seam contract is pinned, so changing it is a deliberate act.
 *
 * `index.ts` is replaced wholesale by a derived deployment, and `Navbar.svelte` imports it
 * statically, so the module's own static import graph is paid by every page load (see the
 * header of `index.ts`). Two things must therefore not drift by accident: the exported
 * symbol list the overlay has to mirror, and the set of files that import the seam statically
 * (a new static importer drags the overlay's graph into another chunk's closure).
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

const SRC = resolve(process.cwd(), 'src');

function hasExportModifier(stmt: ts.Statement): boolean {
  if (!ts.canHaveModifiers(stmt)) return false;
  return ts.getModifiers(stmt)?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword) ?? false;
}

/** The names a `export { a, b }` / `export * from` statement puts in the surface. */
function reExportedNames(stmt: ts.ExportDeclaration): string[] {
  const clause = stmt.exportClause;
  if (clause && ts.isNamedExports(clause)) return clause.elements.map((e) => e.name.text);
  return ['*'];
}

/** The names an `export const/function/interface/type/class` statement declares. */
function declaredNames(stmt: ts.Statement): string[] {
  if (ts.isVariableStatement(stmt)) {
    return stmt.declarationList.declarations.flatMap((d) =>
      ts.isIdentifier(d.name) ? [d.name.text] : []
    );
  }
  const named =
    ts.isFunctionDeclaration(stmt) ||
    ts.isInterfaceDeclaration(stmt) ||
    ts.isTypeAliasDeclaration(stmt) ||
    ts.isClassDeclaration(stmt);
  return named && stmt.name ? [stmt.name.text] : [];
}

/** Every name the seam module exports, values and types alike. */
function seamExports(source: string): string[] {
  const file = ts.createSourceFile('index.ts', source, ts.ScriptTarget.Latest, true);
  const names: string[] = [];
  for (const stmt of file.statements) {
    if (ts.isExportDeclaration(stmt)) names.push(...reExportedNames(stmt));
    else if (hasExportModifier(stmt)) names.push(...declaredNames(stmt));
  }
  return names.sort();
}

/** Specifiers `$lib/cloud...` that *source* imports statically (dynamic and type-only excluded). */
function findStaticCloudImports(source: string): string[] {
  const pattern =
    /^[ \t]*import\s+(?!type\b)(?:[^;'"]*?\s+from\s+)?['"](\$lib\/cloud(?:\/[^'"]*)?)['"]/gm;
  return [...source.matchAll(pattern)].map((m) => m[1]).sort();
}

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full));
    else if (/\.(svelte|ts)$/.test(name) && !/\.test\.ts$/.test(name)) out.push(full);
  }
  return out;
}

// Editing either list below is the deliberate act the contract asks for.
const EXPECTED_SEAM_EXPORTS = [
  'UsageState',
  'externalReauthenticate',
  'externalSignOut',
  'getExternalSessionAuthTime',
  'getSessionToken',
  'hasExternalSession',
  'isOverLimit',
  'loadExternalAuth',
  'mountSignIn',
  'onAuthChange',
  'openAccountPortal',
  'refreshBilling',
  'refreshUsage',
  'showQuotaExceeded',
  'usageStore',
];

const EXPECTED_STATIC_IMPORTERS: Record<string, string[]> = {
  'components/FileUploader.svelte': ['$lib/cloud'],
  'components/Navbar.svelte': [
    '$lib/cloud',
    '$lib/cloud/components/UpgradeButton.svelte',
    '$lib/cloud/components/UsageBadge.svelte',
  ],
  'components/navbar/UserDropdown.svelte': ['$lib/cloud/components/UserMenuExtras.svelte'],
};

describe('seam export list', () => {
  it('matches the documented contract', () => {
    expect(seamExports(readFileSync(join(SRC, 'lib/cloud/index.ts'), 'utf8'))).toEqual(
      EXPECTED_SEAM_EXPORTS
    );
  });

  it('sees a new export (must fire) and ignores non-exports (must stay clean)', () => {
    const src =
      'export async function a() {}\nfunction b() {}\nexport const c = 1;\nexport { d } from "./d";';
    expect(seamExports(src)).toEqual(['a', 'c', 'd']);
  });
});

describe('static importers of the seam', () => {
  it('are exactly the documented files', () => {
    const found: Record<string, string[]> = {};
    for (const file of sourceFiles(SRC)) {
      const rel = relative(SRC, file);
      if (rel.startsWith('lib/cloud/')) continue;
      const hits = findStaticCloudImports(readFileSync(file, 'utf8'));
      if (hits.length > 0) found[rel] = hits;
    }
    expect(found).toEqual(EXPECTED_STATIC_IMPORTERS);
  });

  it('finds a new static import (must fire), multi-line included', () => {
    const src = "<script>\n  import {\n    a,\n    b\n  } from '$lib/cloud';\n</script>";
    expect(findStaticCloudImports(src)).toEqual(['$lib/cloud']);
    expect(findStaticCloudImports('import X from "$lib/cloud/components/X.svelte";')).toEqual([
      '$lib/cloud/components/X.svelte',
    ]);
  });

  it('ignores dynamic, type-only and commented mentions (must stay clean)', () => {
    const src = [
      "const m = await import('$lib/cloud');",
      "import type { UsageState } from '$lib/cloud';",
      " * import { x } from '$lib/cloud' in a doc comment",
      "import { y } from '$lib/cloudy';",
    ].join('\n');
    expect(findStaticCloudImports(src)).toEqual([]);
  });
});
