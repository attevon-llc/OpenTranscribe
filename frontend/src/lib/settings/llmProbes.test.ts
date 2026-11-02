import { describe, it, expect, vi, beforeEach } from 'vitest';
import type {
  ContextWindowCapability,
  ReasoningCapability,
  UserLLMSettings,
} from '$lib/api/llmSettings';

const api = vi.hoisted(() => ({
  testConfiguration: vi.fn(),
  probeReasoningCapability: vi.fn(),
  probeContextWindow: vi.fn(),
}));
vi.mock('$lib/api/llmSettings', async () => {
  const actual =
    await vi.importActual<typeof import('$lib/api/llmSettings')>('$lib/api/llmSettings');
  return { ...actual, LLMSettingsApi: { ...actual.LLMSettingsApi, ...api } };
});

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import {
  contextWindowToast,
  reasoningProbeToast,
  runConnectionTest,
  runContextWindowProbe,
  runReasoningProbe,
} from './llmProbes';

const config = { uuid: 'c1', name: 'Local vLLM' } as UserLLMSettings;
const t = (key: string, params?: Record<string, unknown>) =>
  params ? `${key}${JSON.stringify(params)}` : key;
const reasoning = (off_switch: string) => ({ off_switch }) as unknown as ReasoningCapability;
const window_ = (o: Partial<ContextWindowCapability>) =>
  ({ status: 'measured', discoverable: true, detail: '', ...o }) as ContextWindowCapability;

beforeEach(() => vi.clearAllMocks());

describe('reasoningProbeToast', () => {
  it('is a success only when the off switch works', () => {
    expect(reasoningProbeToast(config, reasoning('works'), t).level).toBe('success');
    expect(reasoningProbeToast(config, reasoning('absent'), t).level).toBe('info');
  });
  it('prefixes the configuration name and falls back for an unknown verdict', () => {
    const toastSpec = reasoningProbeToast(config, reasoning('something-new'), t);
    expect(toastSpec.text.startsWith('Local vLLM: ')).toBe(true);
    expect(toastSpec.durationMs).toBe(8000);
  });
});

describe('contextWindowToast', () => {
  it('a matching window is a success carrying both numbers', () => {
    const spec = contextWindowToast(
      config,
      window_({ relation: 'match', context_window: 65536, configured_max_tokens: 65536 }),
      t
    );
    expect(spec.level).toBe('success');
    expect(spec.text).toContain('"window":65536');
    expect(spec.text).toContain('"configured":65536');
  });
  it.each(['below', 'above'] as const)(
    'a %s mismatch is a longer warning, not a success',
    (relation) => {
      const spec = contextWindowToast(
        config,
        window_({ relation, context_window: 1, configured_max_tokens: 2 }),
        t
      );
      expect(spec.level).toBe('warning');
      expect(spec.durationMs).toBe(10000);
    }
  );
  it('an unmeasured result is informational', () => {
    const spec = contextWindowToast(config, window_({ status: 'unreachable' as never }), t);
    expect(spec.level).toBe('info');
  });
  it('a measurement without a relation is informational, not a mismatch', () => {
    expect(contextWindowToast(config, window_({ relation: null }), t).level).toBe('info');
  });
});

describe('runConnectionTest', () => {
  it('toasts the verdict under the configuration name', async () => {
    api.testConfiguration.mockResolvedValueOnce({ success: true, message: 'ok' });
    await runConnectionTest(config, t);
    expect(toast.success).toHaveBeenCalledWith('Local vLLM: ok', 5000);
    api.testConfiguration.mockResolvedValueOnce({ success: false, message: 'refused' });
    await runConnectionTest(config, t);
    expect(toast.error).toHaveBeenCalledWith('Local vLLM: refused', 8000);
  });
  it('turns a thrown error into an error toast instead of rejecting', async () => {
    api.testConfiguration.mockRejectedValueOnce({ response: { data: { detail: 'boom' } } });
    await expect(runConnectionTest(config, t)).resolves.toBeUndefined();
    expect(toast.error).toHaveBeenCalledWith('Local vLLM: boom', 8000);
  });
});

describe('runReasoningProbe', () => {
  it('announces the probe before the verdict, since it costs three generations', async () => {
    api.probeReasoningCapability.mockResolvedValueOnce({ off_switch: 'works' });
    await runReasoningProbe(config, t);
    expect(toast.info).toHaveBeenCalledWith(
      'settings.llmProvider.probeReasoningRunning{"name":"Local vLLM"}',
      6000
    );
    expect(toast.success).toHaveBeenCalledTimes(1);
    expect(toast.info.mock.invocationCallOrder[0]).toBeLessThan(
      toast.success.mock.invocationCallOrder[0]
    );
  });
  it('reports a failed probe as an error', async () => {
    api.probeReasoningCapability.mockRejectedValueOnce(new Error('timeout'));
    await runReasoningProbe(config, t);
    expect(toast.error).toHaveBeenCalledWith('Local vLLM: timeout', 8000);
  });
});

describe('runContextWindowProbe', () => {
  it('toasts the comparison', async () => {
    api.probeContextWindow.mockResolvedValueOnce(
      window_({ relation: 'below', context_window: 100, configured_max_tokens: 8192 })
    );
    await runContextWindowProbe(config, t);
    expect(toast.warning).toHaveBeenCalledWith(expect.stringContaining('Local vLLM: '), 10000);
  });
  it('reports a failed probe as an error', async () => {
    api.probeContextWindow.mockRejectedValueOnce(new Error('no route'));
    await runContextWindowProbe(config, t);
    expect(toast.error).toHaveBeenCalledWith('Local vLLM: no route', 8000);
  });
});
