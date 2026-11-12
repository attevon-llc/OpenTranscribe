import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyWithOptionsLocale({ code: 'en' })
);
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());
const api = vi.hoisted(() => ({
  getMyUsage: vi.fn(),
  getLlmBudget: vi.fn(),
  updateLlmBudget: vi.fn(),
}));
vi.mock('$lib/api/llmUsage', () => api);

import { toastStore } from '$stores/toast';
import LLMBudgetAdminCard from './LLMBudgetAdminCard.svelte';

const SETTINGS = {
  user_daily_tokens: 0,
  instance_monthly_tokens: 1_000_000,
  local_unlimited: false,
  instance_used_this_month: 250_000,
  month_resets_at: '2026-12-01T00:00:00+00:00',
  day_resets_at: '2026-11-08T00:00:00+00:00',
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getLlmBudget.mockResolvedValue({ ...SETTINGS });
});

async function renderCard(events: Record<string, (e: CustomEvent) => void> = {}) {
  const result = render(LLMBudgetAdminCard, { events } as never);
  const daily = (await result.findByTestId('llm-budget-daily')) as HTMLInputElement;
  const save = result.getByText('settings.llmUsage.admin.save') as HTMLButtonElement;
  return { ...result, daily, save };
}

describe('LLMBudgetAdminCard', () => {
  it('shows the month against the instance limit', async () => {
    const { container } = await renderCard();
    expect(container.textContent).toContain('"used":"250,000","limit":"1,000,000"');
    expect(container.querySelector('.progress-fill')?.getAttribute('style')).toContain(
      'width: 25%'
    );
  });

  it('saves a new daily limit and keeps the others', async () => {
    api.updateLlmBudget.mockResolvedValue({ ...SETTINGS, user_daily_tokens: 50_000 });
    const saved = vi.fn();
    const { daily, save } = await renderCard({ saved });
    expect(save.disabled).toBe(true); // nothing changed yet

    await fireEvent.input(daily, { target: { value: '50000' } });
    expect(save.disabled).toBe(false);
    await fireEvent.click(save);

    await vi.waitFor(() => expect(saved).toHaveBeenCalledTimes(1));
    expect(api.updateLlmBudget).toHaveBeenCalledWith({
      user_daily_tokens: 50_000,
      instance_monthly_tokens: 1_000_000,
      local_unlimited: false,
    });
    expect(toastStore.success).toHaveBeenCalledWith('settings.llmUsage.admin.saved');
  });

  it('refuses a negative limit before anything is sent', async () => {
    const { daily, save, findByRole } = await renderCard();
    await fireEvent.input(daily, { target: { value: '-5' } });
    expect((await findByRole('alert')).textContent).toBe('settings.llmUsage.admin.invalid');
    expect(save.disabled).toBe(true);
    expect(api.updateLlmBudget).not.toHaveBeenCalled();
  });
});
