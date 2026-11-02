/** Run the self-test fixtures against the scanner (audit the auditor). */

import { scanSource } from './scan.mjs';
import { ALLOWLIST_CASES, SELFTEST_CASES, SELFTEST_CLEAN } from './selftest-cases.mjs';

export function runSelfTest() {
  let failures = 0;
  console.log('\n\x1b[1maudit-frontend-tests self-test\x1b[0m\n');
  for (const [label, check] of ALLOWLIST_CASES) {
    let ok = false;
    try {
      ok = check() === true;
    } catch (err) {
      console.log(`      threw: ${err.message}`);
    }
    console.log(`  ${ok ? '\x1b[32m✓' : '\x1b[31m✗'}\x1b[0m allowlist: ${label}`);
    if (!ok) failures += 1;
  }
  for (const [category, source] of SELFTEST_CASES) {
    const { findings } = scanSource(source, 'fixture.test.ts');
    const hit = findings.some((f) => f.category === category);
    console.log(`  ${hit ? '\x1b[32m✓' : '\x1b[31m✗'}\x1b[0m fires ${category}`);
    if (!hit) {
      failures += 1;
      console.log(`      got: ${JSON.stringify(findings.map((f) => f.category))}`);
    }
  }
  for (const [i, source] of SELFTEST_CLEAN.entries()) {
    const { findings } = scanSource(source, 'fixture.test.ts');
    const ok = findings.length === 0;
    console.log(
      `  ${ok ? '\x1b[32m✓' : '\x1b[31m✗'}\x1b[0m clean case ${i + 1} produces no finding`
    );
    if (!ok) {
      failures += 1;
      console.log(
        `      got: ${JSON.stringify(findings.map((f) => `${f.category}: ${f.detail}`))}`
      );
    }
  }
  if (failures) {
    console.log(`\n\x1b[31m${failures} self-test failure(s) — a detector is broken\x1b[0m\n`);
    return 1;
  }
  const total = SELFTEST_CASES.length + SELFTEST_CLEAN.length + ALLOWLIST_CASES.length;
  console.log(`\n\x1b[32mall ${total} self-test cases pass\x1b[0m\n`);
  return 0;
}
