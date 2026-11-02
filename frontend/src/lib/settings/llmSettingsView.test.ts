import { describe, it, expect } from 'vitest';
import type { UserLLMSettings } from '$lib/api/llmSettings';
import {
  connectionFromStoredTest,
  isNoConfigurationsAnswer,
  loadFailureMessage,
  resolveActiveConfiguration,
} from './llmSettingsView';

const cfg = (o: Partial<UserLLMSettings>) => ({ uuid: 'x', name: 'x', ...o }) as UserLLMSettings;

describe('resolveActiveConfiguration', () => {
  const saved = [cfg({ uuid: 'a', name: 'mine' })];
  const shared = [cfg({ uuid: 'a', name: 'theirs' }), cfg({ uuid: 'b', name: 'shared-only' })];
  it('prefers the user own configuration over a shared one', () => {
    expect(resolveActiveConfiguration(saved, shared, 'a')?.name).toBe('mine');
    expect(resolveActiveConfiguration(saved, shared, 'b')?.name).toBe('shared-only');
  });
  it('is null without an active id or when the id matches nothing', () => {
    expect(resolveActiveConfiguration(saved, shared, null)).toBeNull();
    expect(resolveActiveConfiguration(saved, shared, 'zzz')).toBeNull();
  });
});

describe('connectionFromStoredTest', () => {
  it('reads a stored success as connected, with the message and test time', () => {
    const state = connectionFromStoredTest(
      cfg({ test_status: 'success', test_message: 'ok', last_tested: '2026-01-02T03:04:05Z' })
    );
    expect(state).toEqual({
      status: 'connected',
      message: 'ok',
      lastChecked: new Date('2026-01-02T03:04:05Z'),
    });
  });
  it('reads a stored failure as disconnected, with no time when never tested', () => {
    expect(connectionFromStoredTest(cfg({ test_status: 'failed' }))).toEqual({
      status: 'disconnected',
      message: '',
      lastChecked: null,
    });
  });
  it('treats untested, pending and no configuration as unknown', () => {
    expect(connectionFromStoredTest(cfg({ test_status: 'untested' }))).toEqual({
      status: 'unknown',
    });
    expect(connectionFromStoredTest(cfg({ test_status: 'pending' }))).toEqual({
      status: 'unknown',
    });
    expect(connectionFromStoredTest(null)).toEqual({ status: 'unknown' });
  });
});

describe('isNoConfigurationsAnswer', () => {
  it('is true for 404 and 403 only', () => {
    expect(isNoConfigurationsAnswer({ response: { status: 404 } })).toBe(true);
    expect(isNoConfigurationsAnswer({ response: { status: 403 } })).toBe(true);
    expect(isNoConfigurationsAnswer({ response: { status: 500 } })).toBe(false);
    expect(isNoConfigurationsAnswer(new Error('network'))).toBe(false);
    expect(isNoConfigurationsAnswer(null)).toBe(false);
  });
});

describe('loadFailureMessage', () => {
  it('uses the server detail, else the fallback', () => {
    expect(
      loadFailureMessage({ response: { data: { detail: 'Provider down' } } }, 'fallback')
    ).toBe('Provider down');
    expect(loadFailureMessage(new Error('socket hang up'), 'fallback')).toBe('socket hang up');
    expect(loadFailureMessage({}, 'fallback')).toBe('fallback');
  });
  it('stays quiet when the error already talks about the LLM or a configuration', () => {
    expect(loadFailureMessage(new Error('LLM not configured'), 'fallback')).toBeNull();
    expect(
      loadFailureMessage({ response: { data: { detail: 'No configuration found' } } }, 'fallback')
    ).toBeNull();
  });
});
