/**
 * Pure rules behind the prompt library panel (`PromptSettings.svelte`): which prompts are
 * the user's own versus shared by others, which one becomes active by default, what happens
 * to the active selection when a prompt is deleted.
 */
import type { SummaryPrompt } from '$lib/api/prompts';

/**
 * Shared prompts are non-owner, non-system ones. The server returns both in one list, with
 * `is_owner: false` on someone else's; a missing `is_owner` counts as the user's own.
 */
export function splitLoadedPrompts(loaded: SummaryPrompt[]): {
  own: SummaryPrompt[];
  shared: SummaryPrompt[];
} {
  return {
    own: loaded.filter((p) => p.is_system_default || p.is_owner !== false),
    shared: loaded.filter((p) => !p.is_system_default && p.is_owner === false),
  };
}

/**
 * The prompt to activate when the user has none: the "universal"/"general" system prompt,
 * else the first system prompt, else nothing.
 */
export function defaultPromptId(own: SummaryPrompt[]): string | null {
  const universal = own.find(
    (p) =>
      p.is_system_default &&
      (p.name.toLowerCase().includes('universal') || p.name.toLowerCase().includes('general'))
  );
  if (universal) return universal.uuid;
  return own.find((p) => p.is_system_default)?.uuid ?? null;
}

/** The prompt object for an id, looked up in the user's own prompts first, then shared ones. */
export function findPromptById(
  id: string,
  own: SummaryPrompt[],
  shared: SummaryPrompt[]
): SummaryPrompt | null {
  return own.find((p) => p.uuid === id) || shared.find((p) => p.uuid === id) || null;
}

/** System prompts shown in the panel; speaker identification is internal, so it is hidden. */
export function visibleSystemPrompts(own: SummaryPrompt[]): SummaryPrompt[] {
  return own.filter((p) => p.is_system_default && p.content_type !== 'speaker_identification');
}

export function userPromptsOf(own: SummaryPrompt[]): SummaryPrompt[] {
  return own.filter((p) => !p.is_system_default);
}

/**
 * What the active selection needs after the user deleted a prompt (`remaining` is the list
 * with it already removed).
 *
 * - `refetch`: no user prompts remain, so the server falls back to the built-in default and
 *   the panel must ask which one that is, whichever prompt had been active.
 * - `clear`: user prompts remain but the deleted one was active; just drop the selection.
 * - `keep`: the active prompt is unaffected.
 */
export function activeAfterDelete(
  remaining: SummaryPrompt[],
  deletedWasActive: boolean
): 'refetch' | 'clear' | 'keep' {
  if (userPromptsOf(remaining).length === 0) return 'refetch';
  return deletedWasActive ? 'clear' : 'keep';
}
