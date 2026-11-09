// Mock wiring shared by GrantUsesModal.test.ts and GrantUsesModal.guard.test.ts.
// Test files reference these from async vi.mock factories: vi.mock is hoisted above
// imports, so the factories must import this module lazily to reach the same listUses fn.
import { vi } from 'vitest';

export const listUses = vi.fn();

export const localeStub = {
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
};

export const supportAccessStub = { SupportAccessApi: { listUses } };
