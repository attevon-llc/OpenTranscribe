<!--
  GenderBadge.svelte — icon + translated word for a predicted gender (issue #756).

  Extracted out of `ClusterMemberList.svelte`, which had this exact markup duplicated
  twice (the gender-conflict "outlier" row block and the ordinary majority-member row
  block). #756 asked for a visible word alongside the existing icon-only chip on four
  surfaces; duplicating a THIRD copy of this markup to add it is how the second copy got
  here in the first place.

  The `{#if gender === 'male'}…{:else}…{/if}` binary is safe ONLY because the backend
  wire type is `'male' | 'female' | null` (`schemas/media.py` — see the guard on `gender`
  below, which is what makes the `{:else}` branch exhaustively "female", not a fallback).
  Do not drop the outer guard: `SpeakerClusterCard.svelte`/`ProfilesTab.svelte`/
  `SpeakerInboxItem.svelte` each independently rendered "Female" for a null/unknown value
  before this component existed, because their inline `{:else}` had no such guard.
-->
<script lang="ts">
  import { t } from '$stores/locale';

  // Typed `string` (not a `'male'|'female'` union) because the wire shape is looser than
  // that in places (`SpeakerClusterMember.predicted_gender: string | null`) — the runtime
  // `=== 'male'` / `=== 'female'` checks below are what actually narrow it, and anything
  // else (including 'unknown', '', or a future value) renders nothing rather than
  // defaulting to "female". That was the bug in every one of the four surfaces this
  // component replaces.
  export let gender: string | null | undefined = null;
  /** 0-1 confidence score, shown in the tooltip when present. */
  export let confidence: number | null | undefined = null;
  export let confirmed: boolean = false;
</script>

{#if gender === 'male' || gender === 'female'}
  <span
    class="gender-icon"
    title="{gender === 'male' ? $t('speakers.member.male') : $t('speakers.member.female')}{confidence !=
    null
      ? ` (${(confidence * 100).toFixed(0)}%)`
      : ''}"
  >
    {#if gender === 'male'}
      <svg class="gender-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="14" r="7"/><line x1="15" y1="9" x2="21" y2="3"/><polyline points="15 3 21 3 21 9"/></svg>
      <span class="gender-label">{$t('speakers.member.male')}</span>
    {:else}
      <svg class="gender-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="9" r="7"/><line x1="12" y1="16" x2="12" y2="23"/><line x1="9" y1="20" x2="15" y2="20"/></svg>
      <span class="gender-label">{$t('speakers.member.female')}</span>
    {/if}
    {#if confirmed}<span class="gender-confirmed-tick" title={$t('speakers.member.genderConfirmed')}>{'✓'}</span>{/if}
  </span>
{/if}

<style>
  .gender-icon {
    font-size: 12px;
    /* Rendered inside the cluster card's tinted gender chip, where --text-secondary
     * measures 4.25:1 against the success tint — under WCAG AA for this 11px label. */
    color: var(--text-on-tint, #475569);
    display: inline-flex;
    align-items: center;
    gap: 2px;
    line-height: 1;
    vertical-align: middle;
  }

  .gender-svg {
    width: 12px;
    height: 12px;
    flex-shrink: 0;
  }

  .gender-label {
    font-size: 11px;
  }

  .gender-confirmed-tick {
    font-size: 10px;
    color: var(--success-color, #10b981);
    margin-left: 1px;
  }

  @media (max-width: 768px) {
    .gender-icon {
      font-size: 11px;
    }
  }
</style>
