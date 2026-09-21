/**
 * GH #970, reusing #964/#960's exhaustive-Record pattern (see `chatErrors.ts` /
 * `mediaErrors.ts`) rather than inventing a third shape.
 *
 * `LLMSettings.svelte`'s reasoning-off-switch probe (#64) and context-window probe (#533)
 * both built their toast copy by interpolating a measured verdict straight into a template
 * literal. `ReasoningOffSwitch` and `ContextWindowStatus` (`$lib/api/llmSettings.ts`) are
 * backend-owned wire vocabularies — exactly the construct that made #964 render nine raw
 * keys. Each `Record<..., string>` below has no index signature, so TypeScript refuses to
 * compile if either union gains a member with no mapping here.
 *
 * `ContextWindowCapability.relation` isn't exported as its own named type (it's inlined on
 * the interface), so `ContextWindowRelation` is declared here to match it structurally —
 * still exhaustive, since a change to the inline union on the interface that isn't mirrored
 * here fails `resolveContextWindowRelationI18nKey`'s only caller to typecheck.
 */
import type { ReasoningOffSwitch, ContextWindowStatus } from '$lib/api/llmSettings';

const REASONING_OFF_SWITCH_I18N_KEY: Record<ReasoningOffSwitch, string> = {
  unknown: 'settings.llmProvider.reasoningOffSwitch.unknown',
  unsupported: 'settings.llmProvider.reasoningOffSwitch.unsupported',
  no_reasoning: 'settings.llmProvider.reasoningOffSwitch.no_reasoning',
  absent: 'settings.llmProvider.reasoningOffSwitch.absent',
  works: 'settings.llmProvider.reasoningOffSwitch.works',
};

export function resolveReasoningOffSwitchI18nKey(offSwitch: ReasoningOffSwitch): string {
  return REASONING_OFF_SWITCH_I18N_KEY[offSwitch];
}

/** Mirrors `ContextWindowCapability.status` in `$lib/api/llmSettings.ts`. */
const CONTEXT_WINDOW_STATUS_I18N_KEY: Record<ContextWindowStatus, string> = {
  unknown: 'settings.llmProvider.contextWindow.unknown',
  unsupported: 'settings.llmProvider.contextWindow.unsupported',
  not_found: 'settings.llmProvider.contextWindow.not_found',
  unreachable: 'settings.llmProvider.contextWindow.unreachable',
  // The component only reaches this map when `status !== 'measured'` OR `relation` is
  // missing despite a 'measured' status — an edge case the type allows even though the
  // component's normal path routes a measured verdict through the relation map below.
  measured: 'settings.llmProvider.contextWindow.measured',
};

export function resolveContextWindowStatusI18nKey(status: ContextWindowStatus): string {
  return CONTEXT_WINDOW_STATUS_I18N_KEY[status];
}

/** Mirrors `ContextWindowCapability.relation` in `$lib/api/llmSettings.ts`. */
export type ContextWindowRelation = 'below' | 'above' | 'match';

const CONTEXT_WINDOW_RELATION_I18N_KEY: Record<ContextWindowRelation, string> = {
  below: 'settings.llmProvider.contextWindow.measured_below',
  above: 'settings.llmProvider.contextWindow.measured_above',
  match: 'settings.llmProvider.contextWindow.measured_match',
};

export function resolveContextWindowRelationI18nKey(relation: ContextWindowRelation): string {
  return CONTEXT_WINDOW_RELATION_I18N_KEY[relation];
}
