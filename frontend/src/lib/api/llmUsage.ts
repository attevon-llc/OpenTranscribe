/**
 * LLM usage and token limits (issue #790): `GET /usage/me` for anyone, `/admin/llm-budget` for
 * admins. Limits are tokens; `0` means unlimited. Costs are estimates and `null` when unpriced.
 */
import axiosInstance from '$lib/axios';

/** `usage_event.event_type` of an LLM feature's token record. */
export type LlmFeatureEventType =
  | 'chat.tokens'
  | 'summary.tokens'
  | 'topics.tokens'
  | 'speaker_id.tokens'
  | 'redaction.tokens';

export interface LlmTokenBucket {
  events: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number | null;
}

export interface LlmFeatureUsage extends LlmTokenBucket {
  event_type: string;
}

export interface UsageBudget {
  user_daily_tokens: number;
  used_today: number;
  day_resets_at: string;
  instance_limited: boolean;
  instance_limit_reached: boolean;
  month_resets_at: string;
  local_unlimited: boolean;
}

export interface MyUsage {
  window_days: number;
  totals: LlmTokenBucket & { messages: number };
  by_feature: LlmFeatureUsage[];
  estimated_cost_usd: number | null;
  cost_incomplete: boolean;
  rates_verified_on: string;
  budget: UsageBudget;
}

export interface LlmBudgetSettings {
  user_daily_tokens: number;
  instance_monthly_tokens: number;
  local_unlimited: boolean;
  instance_used_this_month: number;
  month_resets_at: string;
  day_resets_at: string;
}

export type LlmBudgetUpdate = Partial<
  Pick<LlmBudgetSettings, 'user_daily_tokens' | 'instance_monthly_tokens' | 'local_unlimited'>
>;

export async function getMyUsage(days = 30): Promise<MyUsage> {
  const { data } = await axiosInstance.get<MyUsage>('/usage/me', { params: { days } });
  return data;
}

export async function getLlmBudget(): Promise<LlmBudgetSettings> {
  const { data } = await axiosInstance.get<LlmBudgetSettings>('/admin/llm-budget');
  return data;
}

export async function updateLlmBudget(update: LlmBudgetUpdate): Promise<LlmBudgetSettings> {
  const { data } = await axiosInstance.put<LlmBudgetSettings>('/admin/llm-budget', update);
  return data;
}
