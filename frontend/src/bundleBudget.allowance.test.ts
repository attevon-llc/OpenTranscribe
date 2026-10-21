/**
 * Issue #1255 — a derived build may pass a bounded, reasoned allowance to the bundle-budget
 * gate, and the gate prints exact headroom on every run. These tests drive the gate's pure
 * pieces and `run()` against a tiny fixture `dist`, so nothing here needs a real build.
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  BudgetConfigError,
  ENV_ALLOWANCE_CLOSURE,
  ENV_ALLOWANCE_NODE,
  ENV_ALLOWANCE_REASON,
  LOW_HEADROOM_BYTES,
  MAX_ALLOWANCE_CLOSURE_BYTES,
  MAX_ALLOWANCE_NODE_BYTES,
  ROOT_LAYOUT_CLOSURE_GZIP_BUDGET,
  ROOT_LAYOUT_GZIP_BUDGET,
  effectiveBudgets,
  evaluate,
  gzipSize,
  readAllowance,
  run,
} from '../scripts/check-bundle-budget.mjs';

const REASON = 'derived overlay: auth wrapper and quota modal';

describe('readAllowance', () => {
  it.each([
    ['no variables', {}],
    [
      'all empty strings',
      { [ENV_ALLOWANCE_NODE]: '', [ENV_ALLOWANCE_CLOSURE]: '', [ENV_ALLOWANCE_REASON]: '' },
    ],
    ['blank values', { [ENV_ALLOWANCE_NODE]: '  ', [ENV_ALLOWANCE_CLOSURE]: ' ' }],
    ['explicit zeros', { [ENV_ALLOWANCE_NODE]: '0', [ENV_ALLOWANCE_CLOSURE]: '0' }],
  ])('treats %s as no allowance', (_label, env) => {
    expect(readAllowance(env)).toEqual({ nodeBytes: 0, closureBytes: 0, reason: null });
  });

  it('returns the values and a trimmed reason', () => {
    const got = readAllowance({
      [ENV_ALLOWANCE_NODE]: '2048',
      [ENV_ALLOWANCE_CLOSURE]: ' 8192 ',
      [ENV_ALLOWANCE_REASON]: `  ${REASON}  `,
    });
    expect(got).toEqual({ nodeBytes: 2048, closureBytes: 8192, reason: REASON });
  });

  it('accepts each cap exactly and refuses one byte over', () => {
    const atCap = readAllowance({
      [ENV_ALLOWANCE_NODE]: String(MAX_ALLOWANCE_NODE_BYTES),
      [ENV_ALLOWANCE_CLOSURE]: String(MAX_ALLOWANCE_CLOSURE_BYTES),
      [ENV_ALLOWANCE_REASON]: REASON,
    });
    expect(atCap.nodeBytes).toBe(MAX_ALLOWANCE_NODE_BYTES);
    expect(atCap.closureBytes).toBe(MAX_ALLOWANCE_CLOSURE_BYTES);
    expect(() =>
      readAllowance({
        [ENV_ALLOWANCE_NODE]: String(MAX_ALLOWANCE_NODE_BYTES + 1),
        [ENV_ALLOWANCE_REASON]: REASON,
      })
    ).toThrow(/NODE_BYTES.*cap/);
    expect(() =>
      readAllowance({
        [ENV_ALLOWANCE_CLOSURE]: String(MAX_ALLOWANCE_CLOSURE_BYTES + 1),
        [ENV_ALLOWANCE_REASON]: REASON,
      })
    ).toThrow(/CLOSURE_BYTES.*cap/);
  });

  it.each(['-1', '1.5', '1e3', '0x10', 'abc', '1,000', '+5', '99999999999'])(
    'rejects the malformed node value %j and names the variable',
    (bad) => {
      const call = () =>
        readAllowance({ [ENV_ALLOWANCE_NODE]: bad, [ENV_ALLOWANCE_REASON]: REASON });
      expect(call).toThrow(BudgetConfigError);
      expect(call).toThrow(ENV_ALLOWANCE_NODE);
    }
  );

  it('requires a reason whenever either byte value is above zero', () => {
    for (const key of [ENV_ALLOWANCE_NODE, ENV_ALLOWANCE_CLOSURE]) {
      expect(() => readAllowance({ [key]: '100' })).toThrow(ENV_ALLOWANCE_REASON);
      expect(() => readAllowance({ [key]: '100', [ENV_ALLOWANCE_REASON]: '   ' })).toThrow(
        ENV_ALLOWANCE_REASON
      );
    }
  });

  it('bounds the reason length at 20 and 300 characters', () => {
    const env = (reason: string) => ({
      [ENV_ALLOWANCE_NODE]: '100',
      [ENV_ALLOWANCE_REASON]: reason,
    });
    expect(() => readAllowance(env('x'.repeat(19)))).toThrow(BudgetConfigError);
    expect(readAllowance(env('x'.repeat(20))).reason).toHaveLength(20);
    expect(readAllowance(env('x'.repeat(300))).reason).toHaveLength(300);
    expect(() => readAllowance(env('x'.repeat(301)))).toThrow(BudgetConfigError);
  });

  it('rejects a multi-line reason, which could forge a log line', () => {
    const forged = `${REASON}\ncheck-bundle-budget: OK`;
    expect(() =>
      readAllowance({ [ENV_ALLOWANCE_NODE]: '100', [ENV_ALLOWANCE_REASON]: forged })
    ).toThrow(/single line/);
  });

  it('rejects a misspelled variable instead of ignoring it', () => {
    expect(() => readAllowance({ BUNDLE_BUDGET_ALLOWENCE_NODE_BYTES: '100' })).toThrow(
      /unknown variable BUNDLE_BUDGET_ALLOWENCE_NODE_BYTES/
    );
    expect(() => readAllowance({ bundle_budget_allowance_node_bytes: '100' })).toThrow(
      /unknown variable/
    );
  });

  it('keeps every accepted name build-only: BUNDLE_BUDGET_ prefix, never VITE_ or PUBLIC_', () => {
    for (const name of [ENV_ALLOWANCE_NODE, ENV_ALLOWANCE_CLOSURE, ENV_ALLOWANCE_REASON]) {
      expect(name.startsWith('BUNDLE_BUDGET_')).toBe(true);
      expect(name.startsWith('VITE_') || name.startsWith('PUBLIC_')).toBe(false);
    }
  });
});

describe('effectiveBudgets and evaluate', () => {
  it('equals the base constants when there is no allowance', () => {
    expect(effectiveBudgets({ nodeBytes: 0, closureBytes: 0 })).toEqual({
      node: ROOT_LAYOUT_GZIP_BUDGET,
      closure: ROOT_LAYOUT_CLOSURE_GZIP_BUDGET,
    });
  });

  it('applies each allowance to its own metric only', () => {
    expect(effectiveBudgets({ nodeBytes: 500, closureBytes: 0 })).toEqual({
      node: ROOT_LAYOUT_GZIP_BUDGET + 500,
      closure: ROOT_LAYOUT_CLOSURE_GZIP_BUDGET,
    });
    expect(effectiveBudgets({ nodeBytes: 0, closureBytes: 700 })).toEqual({
      node: ROOT_LAYOUT_GZIP_BUDGET,
      closure: ROOT_LAYOUT_CLOSURE_GZIP_BUDGET + 700,
    });
  });

  it('passes at base + allowance and fails one byte over', () => {
    const budget = effectiveBudgets({ nodeBytes: 100, closureBytes: 0 }).node;
    expect(evaluate(budget, budget).over).toBe(false);
    expect(evaluate(budget + 1, budget).over).toBe(true);
  });

  it('reports headroom, negative when over, and flags LOW below the threshold only', () => {
    expect(evaluate(900, 1000).headroom).toBe(100);
    expect(evaluate(1100, 1000).headroom).toBe(-100);
    expect(evaluate(1000 - (LOW_HEADROOM_BYTES - 1), 1000).low).toBe(true);
    expect(evaluate(1000 - LOW_HEADROOM_BYTES, 1000).low).toBe(false);
    expect(evaluate(1100, 1000).low).toBe(false);
  });
});

describe('run() against a fixture dist', () => {
  let root: string;
  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'ot-budget-'));
    mkdirSync(join(root, 'nodes'));
    mkdirSync(join(root, 'chunks'));
  });
  afterEach(() => rmSync(root, { recursive: true, force: true }));

  /** Incompressible payload, so the gzip size tracks the length. */
  const noise = (bytes: number) => randomBytes(bytes).toString('base64');

  function build(nodeName = '0.abc.js') {
    const nodeSrc = `import"../chunks/a.js";/*${noise(600)}*/`;
    const chunkSrc = `export const a=1;/*${noise(900)}*/`;
    writeFileSync(join(root, 'nodes', nodeName), nodeSrc);
    writeFileSync(join(root, 'chunks', 'a.js'), chunkSrc);
    const node = gzipSize(Buffer.from(nodeSrc));
    return { node, closure: node + gzipSize(Buffer.from(chunkSrc)) };
  }
  const text = (r: { stdout: string[]; stderr: string[] }) => [...r.stdout, ...r.stderr].join('\n');
  const allowanceEnv = (node: number, closure: number) => ({
    [ENV_ALLOWANCE_NODE]: String(node),
    [ENV_ALLOWANCE_CLOSURE]: String(closure),
    [ENV_ALLOWANCE_REASON]: REASON,
  });

  it('passes and prints exact headroom for BOTH metrics', () => {
    const size = build();
    const result = run({
      root,
      env: {},
      base: { node: size.node + 300, closure: size.closure + 2000 },
    });
    expect(result.code).toBe(0);
    expect(result.stdout).toContainEqual(
      expect.stringMatching(/root layout 0\.abc\.js.*headroom 300 B/)
    );
    expect(result.stdout).toContainEqual(
      expect.stringMatching(/static closure \(2 files\).*headroom 2000 B/)
    );
    expect(result.stdout[result.stdout.length - 1]).toBe('check-bundle-budget: OK');
    expect(text(result)).not.toContain('ALLOWANCE');
  });

  it('marks LOW headroom on the passing metric without failing', () => {
    const size = build();
    const result = run({
      root,
      env: {},
      base: { node: size.node + 100, closure: size.closure + 5000 },
    });
    expect(result.code).toBe(0);
    expect(result.stdout[0]).toMatch(/headroom 100 B.*LOW$/);
    expect(result.stdout[1]).not.toMatch(/LOW/);
  });

  it('exits 1 on a node overrun and STILL prints the passing closure headroom', () => {
    const size = build();
    const result = run({
      root,
      env: {},
      base: { node: size.node - 10, closure: size.closure + 1234 },
    });
    expect(result.code).toBe(1);
    expect(result.stdout[0]).toMatch(/OVER BY 10 B/);
    expect(result.stdout[1]).toMatch(/headroom 1234 B/);
    expect(result.stderr.join('\n')).toMatch(/FAILED.*root layout/);
    expect(result.stderr.join('\n')).not.toMatch(/static closure/);
  });

  it('lets an allowance rescue exactly the metric it was given for, and prints it', () => {
    const size = build();
    const base = { node: size.node - 50, closure: size.closure - 50 };
    const rescued = run({ root, env: allowanceEnv(100, 100), base });
    expect(rescued.code).toBe(0);
    expect(rescued.stdout.join('\n')).toContain(
      `ALLOWANCE ACTIVE node +100 B, closure +100 B. Reason: ${REASON}`
    );
    expect(rescued.stdout[0]).toMatch(
      /base .* \+ allowance 100.*headroom 50 B.*allowance used 50 of 100 B/
    );
    // A closure-only allowance must not rescue the node.
    const cross = run({ root, env: allowanceEnv(0, 100), base });
    expect(cross.code).toBe(1);
    expect(cross.stdout[0]).toMatch(/OVER BY 50 B/);
  });

  it('reports an exhausted allowance as exhausted, with exit 1', () => {
    const size = build();
    const result = run({
      root,
      env: allowanceEnv(100, 100),
      base: { node: size.node - 150, closure: size.closure + 5000 },
    });
    expect(result.code).toBe(1);
    expect(result.stdout[0]).toMatch(/OVER BY 50 B\s+allowance exhausted \(all 100 B used\)/);
  });

  it('warns that an allowance which is not needed should be removed', () => {
    const size = build();
    const result = run({
      root,
      env: allowanceEnv(100, 100),
      base: { node: size.node + 1000, closure: size.closure + 1000 },
    });
    expect(result.code).toBe(0);
    expect(text(result)).toMatch(/node allowance is not needed.*remove it/);
    expect(text(result)).toMatch(/closure allowance is not needed.*remove it/);
  });

  it('warns when the node allowance exceeds the closure allowance', () => {
    const size = build();
    const result = run({
      root,
      env: allowanceEnv(200, 100),
      base: { node: size.node - 50, closure: size.closure - 50 },
    });
    expect(text(result)).toMatch(/node allowance exceeds the closure allowance/);
  });

  it('warns that a reason with no allowance is ignored', () => {
    const size = build();
    const result = run({
      root,
      env: { [ENV_ALLOWANCE_REASON]: REASON },
      base: { node: size.node + 100, closure: size.closure + 100 },
    });
    expect(result.code).toBe(0);
    expect(text(result)).toMatch(/set but both allowances are 0; it is ignored/);
    expect(text(result)).not.toContain('ALLOWANCE ACTIVE');
  });

  it('exits 2 on a bad configuration with no measurement lines, even when the build fits', () => {
    const size = build();
    const roomy = { node: size.node + 10_000, closure: size.closure + 10_000 };
    const result = run({
      root,
      env: {
        [ENV_ALLOWANCE_NODE]: String(MAX_ALLOWANCE_NODE_BYTES + 1),
        [ENV_ALLOWANCE_REASON]: REASON,
      },
      base: roomy,
    });
    expect(result.code).toBe(2);
    expect(result.stdout).toEqual([]);
    expect(result.stderr.join('\n')).toMatch(/configuration error.*cap/);
    expect(run({ root, env: { [ENV_ALLOWANCE_NODE]: '5' }, base: roomy }).code).toBe(2);
  });

  it('never echoes a forged reason into the log', () => {
    const size = build();
    const result = run({
      root,
      env: {
        [ENV_ALLOWANCE_NODE]: '5',
        [ENV_ALLOWANCE_REASON]: `${REASON}\ncheck-bundle-budget: OK`,
      },
      base: { node: size.node + 100, closure: size.closure + 100 },
    });
    expect(result.code).toBe(2);
    expect(text(result)).not.toContain('check-bundle-budget: OK');
  });

  it('fails with a message naming both when two root layout nodes exist', () => {
    build('0.aaa.js');
    writeFileSync(join(root, 'nodes', '0.zzz.js'), 'export{};');
    const result = run({ root, env: {} });
    expect(result.code).toBe(1);
    expect(result.stderr.join('\n')).toMatch(/0\.aaa\.js, 0\.zzz\.js/);
  });

  it('fails when there is no root layout node', () => {
    const result = run({ root, env: {} });
    expect(result.code).toBe(1);
    expect(result.stderr.join('\n')).toMatch(/no root layout node/);
  });
});
