import { describe, it, expect } from 'vitest';
import type { SummaryPrompt } from '$lib/api/prompts';
import {
  blankPromptForm,
  cloneForm,
  isPromptFormDirty,
  isPromptFormValid,
  promptFormFrom,
} from './promptForm';

describe('blankPromptForm', () => {
  it('starts as a general, unshared, empty prompt', () => {
    expect(blankPromptForm()).toEqual({
      name: '',
      description: '',
      prompt_text: '',
      content_type: 'general',
      tags: [],
      is_shared: false,
    });
  });
  it('returns a fresh tags array each time', () => {
    expect(blankPromptForm().tags).not.toBe(blankPromptForm().tags);
  });
});

describe('promptFormFrom', () => {
  it('copies the prompt and defaults missing fields', () => {
    const form = promptFormFrom({
      name: 'N',
      description: null,
      prompt_text: 'T',
      content_type: null,
      tags: undefined,
      is_shared: undefined,
    } as unknown as SummaryPrompt);
    expect(form).toEqual({
      name: 'N',
      description: '',
      prompt_text: 'T',
      content_type: 'general',
      tags: [],
      is_shared: false,
    });
  });
  it('does not alias the prompt tags', () => {
    const prompt = { name: 'N', prompt_text: 'T', tags: ['a'] } as SummaryPrompt;
    promptFormFrom(prompt).tags.push('b');
    expect(prompt.tags).toEqual(['a']);
  });
});

describe('isPromptFormValid', () => {
  it('needs a name and a body once trimmed', () => {
    const ok = { ...blankPromptForm(), name: 'n', prompt_text: 't' };
    expect(isPromptFormValid(ok)).toBe(true);
    expect(isPromptFormValid({ ...ok, name: '   ' })).toBe(false);
    expect(isPromptFormValid({ ...ok, prompt_text: '\n' })).toBe(false);
  });
});

describe('isPromptFormDirty', () => {
  it('is false for an equal copy and true after any edit, including tags', () => {
    const original = { ...blankPromptForm(), name: 'n' };
    const copy = cloneForm(original);
    expect(isPromptFormDirty(copy, original)).toBe(false);
    expect(isPromptFormDirty({ ...copy, description: 'd' }, original)).toBe(true);
    expect(isPromptFormDirty({ ...copy, tags: ['x'] }, original)).toBe(true);
  });
});
