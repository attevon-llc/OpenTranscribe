/** Command-line entry: scan a tree, apply the allowlist, report. */

import { readFileSync, existsSync } from 'node:fs';
import { relative, resolve } from 'node:path';
import {
  AllowlistReasonError,
  BACKLOG_PREFIX,
  applyAllowlist,
  loadAllowlist,
} from './allowlist.mjs';
import { ALLOWLIST_PATH, CATEGORIES, frontendRoot } from './config.mjs';
import { collectTestFiles, scanFile } from './scan.mjs';
import { runSelfTest } from './selftest.mjs';

// ----------------------------------------------------------------------------------- main

export function main(argv) {
  const args = argv.slice(2);
  const flags = new Set(args.filter((a) => a.startsWith('--')));
  const categoryIdx = args.indexOf('--category');
  const category = categoryIdx === -1 ? null : args[categoryIdx + 1];
  const positional = args.filter((a, i) => !a.startsWith('--') && args[i - 1] !== '--category');
  const target = resolve(frontendRoot, positional[0] ?? 'src');

  if (flags.has('--selftest')) return runSelfTest();
  if (category && !CATEGORIES.includes(category)) {
    console.error(`error: unknown category ${category}\n  known: ${CATEGORIES.join(', ')}`);
    return 2;
  }
  if (!existsSync(target)) {
    console.error(`error: ${target} does not exist`);
    return 2;
  }

  const scanRoot = resolve(frontendRoot, 'src');
  const paths = collectTestFiles(target);
  const scans = paths.map((path) => scanFile(path, scanRoot));
  const testsSeen = scans.reduce((n, s) => n + s.testsSeen, 0);
  let findings = scans.flatMap((s) => s.findings);
  if (category) findings = findings.filter((f) => f.category === category);

  let allowed;
  try {
    allowed = existsSync(ALLOWLIST_PATH)
      ? loadAllowlist(readFileSync(ALLOWLIST_PATH, 'utf8'))
      : new Map();
  } catch (err) {
    if (!(err instanceof AllowlistReasonError)) throw err;
    console.error(`error: ${err.message}`);
    return 2;
  }
  const { open, backlog, accepted, stale: allStale } = applyAllowlist(findings, allowed);

  // Staleness is only meaningful on a FULL scan: a `--category` run or a subdirectory has not
  // looked at the findings the other entries cover, and would report every one of them.
  const fullScan = !category && target === scanRoot;
  const stale = fullScan ? allStale : [];
  const all = CATEGORIES;

  if (flags.has('--json')) {
    console.log(
      JSON.stringify(
        {
          files: paths.length,
          tests_seen: testsSeen,
          total: findings.length,
          accepted,
          backlog: backlog.length,
          unallowlisted: open.length,
          stale_allowlist_entries: stale,
          by_category: open.reduce(
            (acc, f) => ({ ...acc, [f.category]: (acc[f.category] ?? 0) + 1 }),
            {}
          ),
          backlog_by_category: backlog.reduce(
            (acc, f) => ({ ...acc, [f.category]: (acc[f.category] ?? 0) + 1 }),
            {}
          ),
          findings: open,
        },
        null,
        2
      )
    );
    return open.length || stale.length ? 1 : 0;
  }

  console.log(
    `\n\x1b[1m${relative(frontendRoot, target) || '.'}\x1b[0m — ${paths.length} test file(s), ` +
      `${testsSeen} tests, ${findings.length} findings: ${open.length} open, ` +
      `${backlog.length} backlog, ${accepted} accepted\n`
  );
  for (const cat of all) {
    const hits = open.filter((f) => f.category === cat);
    const total = findings.filter((f) => f.category === cat).length;
    const deferred = backlog.filter((f) => f.category === cat).length;
    const colour = hits.length ? '\x1b[31m' : '\x1b[32m';
    console.log(
      `  ${colour}${cat.padEnd(26)}\x1b[0m ${String(hits.length).padStart(
        4
      )} open  (${total} total${deferred ? `, ${deferred} backlog` : ''})`
    );
    const show = flags.has('--list') ? hits : hits.slice(0, 5);
    for (const f of show) console.log(`      ${f.path}:${f.line} ${f.test} — ${f.detail}`);
    if (!flags.has('--list') && hits.length > 5)
      console.log(`      … ${hits.length - 5} more (--list)`);
  }

  if (stale.length) {
    console.log(
      `\n\x1b[31m${stale.length} allowlist key(s) hold more entries than findings:\x1b[0m`
    );
    for (const key of stale.slice(0, 20)) console.log(`  ${key}`);
    if (stale.length > 20) console.log(`  … ${stale.length - 20} more`);
    console.log(`  Delete the surplus lines from ${relative(frontendRoot, ALLOWLIST_PATH)}. One`);
    console.log('  line buys one finding: an exemption must never outlive its subject, and a');
    console.log(
      '  key with no finding left silently pre-exempts the next test to reuse the name.\n'
    );
  }
  if (backlog.length) {
    console.log(
      `\x1b[1;33m${backlog.length} finding(s) are DEFERRED WORK, not accepted patterns.\x1b[0m`
    );
    console.log(
      `  They carry a \`${BACKLOG_PREFIX}\` reason in ${relative(frontendRoot, ALLOWLIST_PATH)}.` +
        ' This gate is green\n  because nothing NEW landed — not because the tree is clean.'
    );
  }
  if (open.length) {
    console.log(`\n\x1b[31m${open.length} findings need a fix or an allowlist entry\x1b[0m`);
    console.log(
      `  allowlist: ${relative(frontendRoot, ALLOWLIST_PATH)}` +
        '  (one "<file>::<full test name>::<category>  # reason" per line)\n'
    );
  }
  if (open.length || stale.length) return 1;
  console.log('\n\x1b[32mno un-allowlisted findings\x1b[0m\n');
  return 0;
}
