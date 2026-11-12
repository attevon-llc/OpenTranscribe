<!--
  The summary chips on a cluster card's header row: gender-outlier count, match quality, gender
  composition, member count and the promoted profile. Plain text, no controls.

  Its own component because `SpeakerClusterCard` renders the header row in two shapes (the
  disclosure <button>, and a plain row holding the label <input> while it is being edited: an
  input may not sit inside a button), and both show these chips.
-->
<script lang="ts">
  import type { SpeakerCluster } from '$lib/types/speakerCluster';
  import { t } from '$stores/locale';
  import GenderBadge from './GenderBadge.svelte';

  export let cluster: SpeakerCluster;

  // Computed directly from the cluster data (no API call).
  $: genderMismatchCount = (() => {
    const gc = cluster.gender_composition;
    if (!gc || !gc.has_gender_conflict || gc.total_with_gender < 2) return 0;
    return gc.total_with_gender - Math.max(gc.male_count, gc.female_count);
  })();

  /**
   * Returns a TEXT colour, so it must use the text-weight status tokens rather than the
   * fill ones. `--success-color` (#10b981) on the badge's own success tint measures
   * 2.27:1 — less than half of WCAG AA's 4.5:1 — and the amber and red fills fail the
   * same way. The `--color-*-text` variants exist for exactly this distinction.
   */
  function qualityColor(score: number | null): string {
    if (score === null) return 'var(--text-on-tint)';
    if (score >= 0.8) return 'var(--success-text)';
    if (score >= 0.6) return 'var(--warning-text)';
    return 'var(--error-text)';
  }
</script>

<span class="header-right">
  {#if genderMismatchCount > 0}
    <span class="outlier-chip" title={$t('speakers.cluster.outliersDetected', { count: genderMismatchCount })}>
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
        <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
      </svg>
      {genderMismatchCount}
    </span>
  {/if}
  {#if cluster.quality_score != null && !isNaN(cluster.quality_score)}
    <span class="quality-badge" style="color: {qualityColor(cluster.quality_score)}" title={$t('speakers.tooltip.qualityScore')}>
      {$t('speakers.cluster.matchPercent', { score: (cluster.quality_score * 100).toFixed(0) })}
    </span>
  {/if}
  {#if cluster.gender_composition && cluster.gender_composition.total_with_gender > 0 && (cluster.gender_composition.dominant_gender === 'male' || cluster.gender_composition.dominant_gender === 'female')}
    <span
      class="gender-chip"
      class:gender-coherent={!cluster.gender_composition.has_gender_conflict}
      class:gender-conflict={cluster.gender_composition.has_gender_conflict}
      title={cluster.gender_composition.has_gender_conflict ? $t('speakers.cluster.genderConflict') : $t('speakers.cluster.genderCoherent')}
    >
      <!--
        Composed from the structured `dominant_gender`/`has_gender_conflict` fields
        rather than the backend's `gender_label` string (issue #756) — that string is
        untranslated English ("mostly male") which no locale can render, and the
        backend already sends the structured data this chip needs.
      -->
      <GenderBadge gender={cluster.gender_composition.dominant_gender} />
    </span>
  {/if}
  <span class="member-count" title={$t('speakers.tooltip.memberCount')}>{$t('speakers.cluster.memberCount', { count: cluster.member_count })}</span>
  {#if cluster.promoted_to_profile_name}
    <span class="promoted-badge">{$t('speakers.cluster.profile', { name: cluster.promoted_to_profile_name })}</span>
  {/if}
</span>

<style>
  .header-right {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-shrink: 0;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .quality-badge {
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-xl);
    background: currentcolor;
    background: color-mix(in srgb, currentcolor 12%, transparent);
    white-space: nowrap;
  }

  .gender-chip {
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-xl);
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    line-height: 1;
    vertical-align: middle;
  }

  .gender-coherent {
    background: color-mix(in srgb, var(--success-color) 12%, transparent);
    color: var(--success-text);
  }

  .gender-conflict {
    background: color-mix(in srgb, var(--warning-color) 12%, transparent);
    color: var(--warning-text);
  }

  .member-count {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .promoted-badge {
    font-size: var(--font-size-2xs);
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-xl);
    background: color-mix(in srgb, var(--success-color) 15%, transparent);
    color: var(--success-text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 150px;
  }

  .outlier-chip {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-xl);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    background: color-mix(in srgb, var(--warning-color) 12%, transparent);
    color: var(--warning-text);
    white-space: nowrap;
  }

  @media (max-width: 768px) {
    .header-right {
      flex-shrink: 1;
      min-width: 0;
      gap: var(--space-1-5);
    }

    .quality-badge,
    .gender-chip,
    .outlier-chip {
      font-size: var(--font-size-2xs);
      padding: 1px var(--space-1-5);
    }

    .member-count {
      font-size: var(--font-size-xs);
    }

    .promoted-badge {
      max-width: 100px;
      font-size: var(--font-size-2xs);
      padding: 1px var(--space-1-5);
    }
  }

  @media (max-width: 480px) {
    .header-right {
      flex-basis: 100%;
      justify-content: flex-start;
      margin-top: var(--space-1);
    }

    .promoted-badge {
      max-width: 140px;
    }
  }
</style>
