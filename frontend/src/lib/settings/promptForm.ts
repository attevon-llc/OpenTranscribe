/**
 * The create/edit form of the prompt library panel: its shape, how it is seeded from an
 * existing prompt, and when it counts as valid or changed.
 */
import type { SummaryPrompt } from '$lib/api/prompts';

export interface PromptFormData {
  name: string;
  description: string;
  prompt_text: string;
  content_type: string;
  tags: string[];
  is_shared: boolean;
}

export const DEFAULT_CONTENT_TYPE = 'general';

export function blankPromptForm(): PromptFormData {
  return {
    name: '',
    description: '',
    prompt_text: '',
    content_type: DEFAULT_CONTENT_TYPE,
    tags: [],
    is_shared: false,
  };
}

/** The form for editing `prompt`; null-ish server fields become the empty form defaults. */
export function promptFormFrom(prompt: SummaryPrompt): PromptFormData {
  return {
    name: prompt.name,
    description: prompt.description || '',
    prompt_text: prompt.prompt_text,
    content_type: prompt.content_type || DEFAULT_CONTENT_TYPE,
    tags: [...(prompt.tags || [])],
    is_shared: prompt.is_shared || false,
  };
}

/** An independent copy, so later edits to the form cannot mutate the baseline. */
export function cloneForm(form: PromptFormData): PromptFormData {
  return { ...form, tags: [...form.tags] };
}

/** A prompt needs a name and a body, ignoring surrounding whitespace. */
export function isPromptFormValid(form: PromptFormData): boolean {
  return !!(form.name && form.name.trim() && form.prompt_text && form.prompt_text.trim());
}

/** Whether the form differs from what it was opened with (tags is an array, so JSON compares). */
export function isPromptFormDirty(form: PromptFormData, original: PromptFormData): boolean {
  return JSON.stringify(form) !== JSON.stringify(original);
}
