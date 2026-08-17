/**
 * `SummaryActions.svelte` picks ONE of four mutually-exclusive action states
 * off `{summary, summaryStatus, llmAvailable, canRetry, canGenerate,
 * summaryEnabledSystem}` (see the `{#if}/{:else if}` chain around lines
 * 49-106): a disabled-badge state, a "generate" state, a "retry" state, or
 * nothing at all. This is the same shape as `SettingsModal.svelte`'s
 * `sectionLocked()` — a small pure selector wired through several props —
 * so it's tested the same way: table-driven, one row per combination.
 *
 * `$t` is left unmocked deliberately (as in `RetrievalQualityNotice.test.ts`):
 * i18next is uninitialised in this test environment, so `$t('key')` returns
 * the raw key, which is exactly what lets these tests assert on the label
 * strings without needing real translations loaded.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

import SummaryActions from './SummaryActions.svelte';

type Props = {
  summary: unknown;
  summaryStatus: string;
  llmAvailable: boolean;
  canRetry: boolean;
  canGenerate: boolean;
  summaryEnabledSystem: boolean;
};

function baseProps(overrides: Partial<Props> = {}): Props {
  return {
    summary: null,
    summaryStatus: 'pending',
    llmAvailable: false,
    canRetry: false,
    canGenerate: true,
    summaryEnabledSystem: true,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.get.mockResolvedValue({ data: { prompts: [] } });
});

describe('SummaryActions action selection', () => {
  it.each([
    {
      name: 'disabled status + can generate: shows the disabled badge AND a "generate anyway" button',
      props: baseProps({ summaryStatus: 'disabled', canGenerate: true }),
      expectShown: ['summary.statusDisabled', 'summary.generateAnyway'],
      expectHidden: ['summary.generateSummary', 'summary.retrySummaryGeneration'],
    },
    {
      name: 'disabled status + cannot generate: shows the disabled badge and the admin hint, no button',
      props: baseProps({
        summaryStatus: 'disabled',
        canGenerate: false,
        summaryEnabledSystem: false,
      }),
      expectShown: ['summary.statusDisabled', 'summary.adminEnableHint'],
      expectHidden: ['summary.generateAnyway', 'summary.generateSummary'],
    },
    {
      name: 'disabled status + cannot generate + system enabled: shows only the disabled badge',
      props: baseProps({
        summaryStatus: 'disabled',
        canGenerate: false,
        summaryEnabledSystem: true,
      }),
      expectShown: ['summary.statusDisabled'],
      expectHidden: ['summary.generateAnyway', 'summary.adminEnableHint'],
    },
    {
      name: 'no summary, llm available, pending: shows the primary "generate" button',
      props: baseProps({ summaryStatus: 'pending', llmAvailable: true }),
      expectShown: ['summary.generateSummary'],
      expectHidden: ['summary.statusDisabled', 'summary.retrySummaryGeneration'],
    },
    {
      name: 'no summary, can retry, failed: shows the "retry" button',
      props: baseProps({ summaryStatus: 'failed', canRetry: true }),
      expectShown: ['summary.retrySummaryGeneration'],
      expectHidden: ['summary.generateSummary', 'summary.statusDisabled'],
    },
    {
      name: 'no summary, can retry, error status: also shows the "retry" button',
      props: baseProps({ summaryStatus: 'error', canRetry: true }),
      expectShown: ['summary.retrySummaryGeneration'],
      expectHidden: ['summary.generateSummary'],
    },
    {
      name: 'summary present, llm available: shows the prompt picker + regenerate button, no generate/retry',
      props: baseProps({ summary: { text: 'done' }, llmAvailable: true }),
      expectShown: ['summary.regenerateWithPrompt', 'summary.useActivePrompt'],
      expectHidden: ['summary.generateSummary', 'summary.retrySummaryGeneration'],
    },
    {
      name: 'summary present, llm unavailable: shows nothing (no prompt picker, no action buttons)',
      props: baseProps({ summary: { text: 'done' }, llmAvailable: false }),
      expectShown: [],
      expectHidden: [
        'summary.generateSummary',
        'summary.retrySummaryGeneration',
        'summary.regenerateWithPrompt',
      ],
    },
  ])('$name', async ({ props, expectShown, expectHidden }) => {
    const { container } = render(SummaryActions, { props: props as never });
    const text = container.textContent ?? '';

    for (const key of expectShown) {
      expect(text).toContain(key);
    }
    for (const key of expectHidden) {
      expect(text).not.toContain(key);
    }
  });

  /**
   * Possible gap flagged by a prior code-reading review, not yet confirmed as
   * a bug: `!summary && summaryStatus === 'pending' && !llmAvailable` falls
   * through all four branches of the `{#if}/{:else if}` chain (the
   * 'disabled' branch needs `summaryStatus === 'disabled'`; the 'generate'
   * branch needs `llmAvailable`; the 'retry' branch needs `canRetry` +
   * failed/error status), so the component renders an empty
   * `.summary-actions` div with no button and no explanatory text. This test
   * pins that behavior rather than asserting it is correct — if UX intends a
   * message here (e.g. "summary not available"), this is the case to fix.
   */
  it('renders nothing (no button, no message) when pending with no LLM configured', () => {
    const { container } = render(SummaryActions, {
      props: baseProps({ summary: null, summaryStatus: 'pending', llmAvailable: false }) as never,
    });

    const actionsDiv = container.querySelector('.summary-actions');
    expect(actionsDiv).not.toBeNull();
    expect(actionsDiv?.textContent?.trim()).toBe('');
    expect(container.querySelector('button')).toBeNull();
  });
});
