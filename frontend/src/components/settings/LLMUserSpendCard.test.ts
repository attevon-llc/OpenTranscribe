import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', () => {
  const store = <T>(value: T) => ({
    subscribe: (run: (v: T) => void) => {
      run(value);
      return () => {};
    },
  });
  return {
    t: store((key: string, vars?: Record<string, unknown>) =>
      vars ? `${key} ${JSON.stringify(vars)}` : key
    ),
    locale: store('en'),
  };
});
const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: toast }));
const api = vi.hoisted(() => ({ getLlmSpendPerUser: vi.fn() }));
vi.mock('$lib/api/llmUsage', () => api);

import LLMUserSpendCard from './LLMUserSpendCard.svelte';

const REPORT = {
  rates_verified_on: '2026-08-06',
  users: [
    {
      user_uuid: '00000000-0000-4000-8000-000000000001',
      email: 'heavy@example.com',
      full_name: 'Heavy User',
      tokens_today: 1_500,
      tokens_this_month: 12_345,
      estimated_cost_usd: 1.5,
      cost_incomplete: false,
    },
    {
      user_uuid: '00000000-0000-4000-8000-000000000002',
      email: 'local@example.com',
      full_name: null,
      tokens_today: 0,
      tokens_this_month: 40,
      estimated_cost_usd: 0.25,
      cost_incomplete: true,
    },
    {
      user_uuid: '00000000-0000-4000-8000-000000000003',
      email: 'none@example.com',
      full_name: null,
      tokens_today: 0,
      tokens_this_month: 5,
      estimated_cost_usd: null,
      cost_incomplete: true,
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('LLMUserSpendCard', () => {
  it('lists each user with tokens and an estimated cost, unpriced when unknown', async () => {
    api.getLlmSpendPerUser.mockResolvedValue(REPORT);
    const { findByTestId } = render(LLMUserSpendCard);
    const table = await findByTestId('llm-user-spend-table');
    const rows = table.querySelectorAll('tbody tr');
    expect(rows).toHaveLength(3);
    const [heavy, partial, unpriced] = rows;
    expect(heavy?.textContent).toContain('heavy@example.com');
    expect(heavy?.textContent).toContain('1,500');
    expect(heavy?.textContent).toContain('12,345');
    expect(heavy?.textContent).toContain('$1.50');
    // A partial cost is explained in text, not only by a title tooltip.
    expect(partial?.querySelector('.sr-only')?.textContent).toContain(
      'settings.llmUsage.perUser.partial'
    );
    expect(heavy?.querySelector('.sr-only')).toBeNull();
    expect(unpriced?.textContent).toContain('settings.llmUsage.unpriced');
    expect(unpriced?.querySelector('.sr-only')).toBeNull();
  });

  it('says so when nobody has used AI this month', async () => {
    api.getLlmSpendPerUser.mockResolvedValue({ ...REPORT, users: [] });
    const { findByTestId } = render(LLMUserSpendCard);
    expect((await findByTestId('llm-user-spend-empty')).textContent).toContain(
      'settings.llmUsage.perUser.empty'
    );
  });

  it('toasts when the report cannot be loaded', async () => {
    api.getLlmSpendPerUser.mockRejectedValue(new Error('boom'));
    render(LLMUserSpendCard);
    await vi.waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
  });
});
