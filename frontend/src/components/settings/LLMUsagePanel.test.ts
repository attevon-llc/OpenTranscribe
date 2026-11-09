import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

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
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());

const api = vi.hoisted(() => ({
  getMyUsage: vi.fn(),
  getLlmBudget: vi.fn(),
  updateLlmBudget: vi.fn(),
}));
vi.mock('$lib/api/llmUsage', () => api);

import LLMUsagePanel from './LLMUsagePanel.svelte';

function usage(overrides: Record<string, unknown> = {}) {
  return {
    window_days: 30,
    totals: {
      events: 2,
      messages: 1,
      prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 1300,
      estimated_cost_usd: null,
    },
    by_feature: [
      {
        event_type: 'summary.tokens',
        events: 1,
        prompt_tokens: 0,
        completion_tokens: 0,
        total_tokens: 1000,
        estimated_cost_usd: 0.25,
      },
      {
        event_type: 'chat.tokens',
        events: 1,
        prompt_tokens: 0,
        completion_tokens: 0,
        total_tokens: 300,
        estimated_cost_usd: null,
      },
    ],
    estimated_cost_usd: 0.25,
    cost_incomplete: true,
    rates_verified_on: '2026-08-06',
    budget: {
      user_daily_tokens: 4000,
      used_today: 1000,
      day_resets_at: '2026-11-08T00:00:00+00:00',
      instance_limited: false,
      instance_limit_reached: false,
      month_resets_at: '2026-12-01T00:00:00+00:00',
      local_unlimited: false,
    },
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getLlmBudget.mockResolvedValue({
    user_daily_tokens: 4000,
    instance_monthly_tokens: 0,
    local_unlimited: false,
    instance_used_this_month: 1300,
    month_resets_at: '2026-12-01T00:00:00+00:00',
    day_resets_at: '2026-11-08T00:00:00+00:00',
  });
});

describe('LLMUsagePanel', () => {
  it('sits inside the shared settings form scope so its cards take the sibling card styling', async () => {
    api.getMyUsage.mockResolvedValue(usage());
    const { container, findByText } = render(LLMUsagePanel);

    await findByText('settings.llmUsage.feature.summary');
    const cards = container.querySelectorAll('.tx-form .settings-section');
    expect(cards.length).toBeGreaterThanOrEqual(2);
    expect(container.querySelectorAll('.settings-section').length).toBe(cards.length);
  });

  it('shows today against the daily limit and every feature, with an estimate only where priced', async () => {
    api.getMyUsage.mockResolvedValue(usage());
    const { container, findByText } = render(LLMUsagePanel);

    await findByText('settings.llmUsage.feature.summary');
    expect(container.querySelector('.progress-fill')?.getAttribute('style')).toContain(
      'width: 25%'
    );
    expect(container.textContent).toContain('"used":"1,000","limit":"4,000"');
    const rows = Array.from(container.querySelectorAll('tbody tr')).map((r) =>
      Array.from(r.querySelectorAll('td')).map((c) => c.textContent?.trim())
    );
    expect(rows).toEqual([
      ['settings.llmUsage.feature.summary', '1,000', '$0.25'],
      ['settings.llmUsage.feature.chat', '300', 'settings.llmUsage.unpriced'],
    ]);
    // Some models are unpriced: the total is labelled partial, never presented as complete.
    expect(container.querySelector('[data-testid="llm-usage-cost"]')?.textContent).toContain(
      'settings.llmUsage.costPartial'
    );
  });

  it('says there is no daily limit instead of drawing a bar against zero', async () => {
    api.getMyUsage.mockResolvedValue(
      usage({ budget: { ...usage().budget, user_daily_tokens: 0, used_today: 52 } })
    );
    const { container, findByTestId } = render(LLMUsagePanel);

    const today = await findByTestId('llm-usage-today');
    expect(today.textContent).toContain('settings.llmUsage.usedUnlimited');
    expect(container.querySelector('.progress-fill')).toBeNull();
  });

  it('tells a user when the instance limit stops AI features', async () => {
    api.getMyUsage.mockResolvedValue(
      usage({ budget: { ...usage().budget, instance_limited: true, instance_limit_reached: true } })
    );
    const { findByText } = render(LLMUsagePanel);
    expect(await findByText('settings.llmUsage.instanceLimitReached')).toBeTruthy();
  });

  it('shows the limit editor to admins only', async () => {
    api.getMyUsage.mockResolvedValue(usage());
    const user = render(LLMUsagePanel, { isAdmin: false });
    await user.findByText('settings.llmUsage.feature.summary');
    expect(user.queryByText('settings.llmUsage.admin.title')).toBeNull();
    expect(api.getLlmBudget).not.toHaveBeenCalled();
    user.unmount();

    const admin = render(LLMUsagePanel, { isAdmin: true });
    expect(await admin.findByText('settings.llmUsage.admin.title')).toBeTruthy();
    await waitFor(() => expect(api.getLlmBudget).toHaveBeenCalledTimes(1));
  });

  it('offers a retry when usage cannot be loaded', async () => {
    api.getMyUsage.mockRejectedValueOnce(new Error('down'));
    const { findByRole } = render(LLMUsagePanel);
    expect((await findByRole('alert')).textContent).toBe('settings.llmUsage.loadFailed');
  });
});
