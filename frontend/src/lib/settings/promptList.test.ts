import { describe, it, expect } from 'vitest';
import type { SummaryPrompt } from '$lib/api/prompts';
import {
  activeAfterDelete,
  defaultPromptId,
  findPromptById,
  splitLoadedPrompts,
  userPromptsOf,
  visibleSystemPrompts,
} from './promptList';

const p = (o: Partial<SummaryPrompt>): SummaryPrompt =>
  ({
    uuid: 'x',
    name: 'x',
    is_system_default: false,
    content_type: 'general',
    ...o,
  }) as SummaryPrompt;

describe('splitLoadedPrompts', () => {
  it('puts system, own and owner-unknown prompts in own; others in shared', () => {
    const { own, shared } = splitLoadedPrompts([
      p({ uuid: 'sys', is_system_default: true, is_owner: false }),
      p({ uuid: 'mine', is_owner: true }),
      p({ uuid: 'legacy' }),
      p({ uuid: 'theirs', is_owner: false }),
    ]);
    expect(own.map((x) => x.uuid)).toEqual(['sys', 'mine', 'legacy']);
    expect(shared.map((x) => x.uuid)).toEqual(['theirs']);
  });
});

describe('defaultPromptId', () => {
  it('prefers a universal or general system prompt', () => {
    expect(
      defaultPromptId([
        p({ uuid: 'first', is_system_default: true, name: 'Meeting' }),
        p({ uuid: 'uni', is_system_default: true, name: 'Universal Content Analyzer' }),
      ])
    ).toBe('uni');
  });
  it('falls back to the first system prompt, and ignores user prompts named universal', () => {
    expect(
      defaultPromptId([
        p({ uuid: 'mine', name: 'universal' }),
        p({ uuid: 'sys1', is_system_default: true, name: 'Meeting' }),
        p({ uuid: 'sys2', is_system_default: true, name: 'Podcast' }),
      ])
    ).toBe('sys1');
  });
  it('is null with no system prompt', () => {
    expect(defaultPromptId([p({ uuid: 'mine' })])).toBeNull();
    expect(defaultPromptId([])).toBeNull();
  });
});

describe('findPromptById', () => {
  it('looks in own prompts before shared ones, else null', () => {
    const own = [p({ uuid: 'a', name: 'own' })];
    const shared = [p({ uuid: 'a', name: 'shared' }), p({ uuid: 'b', name: 'only-shared' })];
    expect(findPromptById('a', own, shared)?.name).toBe('own');
    expect(findPromptById('b', own, shared)?.name).toBe('only-shared');
    expect(findPromptById('c', own, shared)).toBeNull();
  });
});

describe('visibleSystemPrompts / userPromptsOf', () => {
  const all = [
    p({ uuid: 's1', is_system_default: true }),
    p({ uuid: 's2', is_system_default: true, content_type: 'speaker_identification' }),
    p({ uuid: 'u1' }),
  ];
  it('hides the internal speaker-identification prompt', () => {
    expect(visibleSystemPrompts(all).map((x) => x.uuid)).toEqual(['s1']);
  });
  it('lists only non-system prompts as the user ones', () => {
    expect(userPromptsOf(all).map((x) => x.uuid)).toEqual(['u1']);
  });
});

describe('activeAfterDelete', () => {
  const sys = p({ uuid: 's', is_system_default: true });
  const mine = p({ uuid: 'm' });
  it('refetches the fallback when no user prompt remains, active or not', () => {
    expect(activeAfterDelete([sys], true)).toBe('refetch');
    expect(activeAfterDelete([sys], false)).toBe('refetch');
  });
  it('clears the selection only when the deleted prompt was the active one', () => {
    expect(activeAfterDelete([sys, mine], true)).toBe('clear');
    expect(activeAfterDelete([sys, mine], false)).toBe('keep');
  });
});
