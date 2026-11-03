import { describe, it, expect } from 'vitest';
import { bulkLlmFailureKey } from './bulkLlmOutcome';

describe('bulkLlmFailureKey', () => {
  it('prefers "unusable" over "not available" when both appear', () => {
    expect(
      bulkLlmFailureKey([
        { success: false, error: 'LLM_NOT_AVAILABLE' },
        { success: false, error: 'LLM_CONFIG_UNUSABLE' },
      ])
    ).toBe('gallery.bulk.llmConfigUnusable');
  });

  it('reports "not configured" for LLM_NOT_AVAILABLE alone', () => {
    expect(bulkLlmFailureKey([{ success: false, error: 'LLM_NOT_AVAILABLE' }])).toBe(
      'gallery.bulk.llmNotConfigured'
    );
  });

  it('is null when every file succeeded', () => {
    expect(bulkLlmFailureKey([{ success: true }, { success: true, error: null }])).toBeNull();
  });

  it('is null for unrelated failures, and ignores an error code on a successful row', () => {
    expect(
      bulkLlmFailureKey([
        { success: false, error: 'INVALID_STATUS' },
        { success: true, error: 'LLM_CONFIG_UNUSABLE' },
      ])
    ).toBeNull();
  });
});
