<script lang="ts">
  import { focusOnMount } from '$lib/actions/focusOnMount';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { createEventDispatcher } from 'svelte';
  import type { SpeakerCluster } from '$lib/types/speakerCluster';
  import { t } from '$stores/locale';
  import GenderBadge from './GenderBadge.svelte';

  export let cluster: SpeakerCluster;
  export let expanded = false;
  export let actionInProgress = false;
  export let loading = false;
  export let unassignActive = false;
  export let unassignSelectedCount = 0;
  export let unassignTotalCount = 0;
  export let unassignBlacklist = true;

  // Compute gender mismatch count directly from cluster data (no API call)
  $: genderMismatchCount = (() => {
    const gc = cluster.gender_composition;
    if (!gc || !gc.has_gender_conflict || gc.total_with_gender < 2) return 0;
    return gc.total_with_gender - Math.max(gc.male_count, gc.female_count);
  })();

  const dispatch = createEventDispatcher();

  let editingLabel = false;
  let labelInput = cluster.label || '';

  function toggleExpand() {
    expanded = !expanded;
    if (expanded) {
      dispatch('expand', { uuid: cluster.uuid });
    }
  }

  function startEdit() {
    labelInput = cluster.label || '';
    editingLabel = true;
  }

  function startEditOnKey(event: KeyboardEvent) {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    startEdit();
  }

  function saveLabel() {
    editingLabel = false;
    const normalizedLabel = labelInput.trim() || null;
    if (normalizedLabel !== cluster.label) {
      dispatch('update', { uuid: cluster.uuid, label: normalizedLabel });
    }
  }

  function cancelEdit() {
    editingLabel = false;
    labelInput = cluster.label || '';
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') saveLabel();
    if (e.key === 'Escape') cancelEdit();
  }

  /**
   * Returns a TEXT colour, so it must use the text-weight status tokens rather than the
   * fill ones. `--success-color` (#10b981) on the badge's own success tint measures
   * 2.27:1 — less than half of WCAG AA's 4.5:1 — and the amber and red fills fail the
   * same way. The `--color-*-text` variants exist for exactly this distinction.
   */
  function qualityColor(score: number | null): string {
    if (score === null) return 'var(--text-on-tint)';
    if (score >= 0.8) return 'var(--color-success-text, #047857)';
    if (score >= 0.6) return 'var(--color-warning-text, #b45309)';
    return 'var(--color-error-text, #b91c1c)';
  }
</script>

<div class="cluster-card" class:expanded>
  <div class="card-header" on:click={toggleExpand} on:keydown={toggleExpand} role="button" tabindex="0" aria-expanded={expanded} title={$t('speakers.tooltip.expandCluster')}>
    <div class="header-left">
      <span class="expand-icon" aria-hidden="true"><MenuIcon name={expanded ? 'chevron-down' : 'chevron-right'} size="xs" /></span>
      {#if cluster.promoted_to_profile_avatar_url}
        <img
          class="cluster-avatar"
          src={cluster.promoted_to_profile_avatar_url}
          alt=""
          loading="lazy"
          decoding="async"
        />
      {/if}
      {#if editingLabel}
        <input
          class="label-input"
          bind:value={labelInput}
          on:blur={saveLabel}
          on:keydown={handleKeydown}
          on:click|stopPropagation
          use:focusOnMount
          maxlength="200"
          placeholder={$t('speakers.cluster.editLabel')}
        />
      {:else}
        {#if cluster.label || cluster.promoted_to_profile_name}
          <span class="cluster-label" role="button" tabindex="0" on:dblclick|stopPropagation={startEdit} on:keydown={startEditOnKey} title={$t('speakers.tooltip.editLabel')}>
            {cluster.label || cluster.promoted_to_profile_name}
          </span>
        {:else if cluster.suggested_name}
          <span class="cluster-label suggested" role="button" tabindex="0" on:dblclick|stopPropagation={startEdit} on:keydown={startEditOnKey} title={$t('speakers.tooltip.suggestedName')}>
            {cluster.suggested_name}
          </span>
          <span class="suggested-badge" title={$t('speakers.tooltip.suggestedName')}>{$t('speakers.cluster.suggested')}</span>
        {:else}
          <span class="cluster-label" role="button" tabindex="0" on:dblclick|stopPropagation={startEdit} on:keydown={startEditOnKey} title={$t('speakers.tooltip.editLabel')}>
            {$t('speakers.cluster.unlabeled')}
          </span>
        {/if}
      {/if}
    </div>
    <div class="header-right">
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
    </div>
  </div>

  {#if expanded}
    <div class="card-body">
      {#if loading}
        <div class="skeleton-members">
          {#each Array(Math.min(cluster.member_count, 3)) as _, i (i)}
            <div class="skeleton-row">
              <div class="skeleton-avatar"></div>
              <div class="skeleton-text">
                <div class="skeleton-line"></div>
                <div class="skeleton-line short"></div>
              </div>
            </div>
          {/each}
        </div>
      {:else}
        <div class="card-actions">
          {#if unassignActive}
            <button class="action-btn" on:click={() => dispatch('cancelUnassign')}>{$t('modal.cancel')}</button>
            <label class="blacklist-toggle">
              <input type="checkbox" checked={unassignBlacklist} on:change={(e) => dispatch('toggleBlacklist', (e.target as HTMLInputElement).checked)} />
              {$t('speakers.cluster.unassignBlacklist')}
            </label>
            <span class="selection-chip">{$t('speakers.unassign.selectMembersCount', { selected: unassignSelectedCount, total: unassignTotalCount })}</span>
            <button class="action-btn confirm-unassign" disabled={unassignSelectedCount === 0} on:click={() => dispatch('confirmUnassign')}>
              {$t('speakers.unassign.confirm', { count: unassignSelectedCount })}
            </button>
          {:else}
            {#if !cluster.promoted_to_profile_id}
              <button class="action-btn promote" disabled={actionInProgress} on:click={() => dispatch('promote', { uuid: cluster.uuid })} title={$t('speakers.tooltip.promoteToProfile')}>
                {$t('speakers.promote.title')}
              </button>
            {/if}
            <button class="action-btn merge" disabled={actionInProgress} on:click={() => dispatch('merge', { uuid: cluster.uuid })} title={$t('speakers.tooltip.mergeClusters')}>
              {$t('speakers.cluster.merge')}
            </button>
            {#if cluster.member_count > 1}
              <button class="action-btn split" disabled={actionInProgress} on:click={() => dispatch('split', { uuid: cluster.uuid })} title={$t('speakers.tooltip.splitCluster')}>
                {$t('speakers.cluster.split')}
              </button>
              <button class="action-btn unassign" disabled={actionInProgress} on:click={() => dispatch('unassign', { uuid: cluster.uuid })}>
                {$t('speakers.cluster.unassign')}
              </button>
            {/if}
            <button class="action-btn delete" disabled={actionInProgress} on:click={() => dispatch('delete', { uuid: cluster.uuid })} title={$t('speakers.tooltip.deleteCluster')}>
              {$t('speakers.cluster.deleteBtn')}
            </button>
          {/if}
        </div>
        <slot name="members" />
      {/if}
    </div>
  {/if}
</div>

<style>
  .cluster-card {
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background: var(--card-background);
    overflow: hidden;
    transition: box-shadow var(--duration-fast) ease;
  }

  .cluster-card:hover {
    box-shadow: var(--shadow-sm);
  }

  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-3) var(--space-4);
    cursor: pointer;
    user-select: none;
    gap: var(--space-2);
    min-width: 0;
  }

  .card-header:hover {
    background: var(--hover-color);
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex: 1;
    min-width: 0;
  }

  .expand-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--text-secondary);
    width: 16px;
  }

  .cluster-label {
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cluster-label.suggested {
    font-style: italic;
    color: var(--primary-on-surface);
  }

  .suggested-badge {
    font-size: 10px;
    font-weight: var(--font-weight-semibold);
    padding: 1px var(--space-1-5);
    border-radius: var(--radius-lg);
    background: color-mix(in srgb, var(--primary-color) 12%, transparent);
    color: var(--primary-on-surface);
    white-space: nowrap;
    flex-shrink: 0;
  }

  .label-input {
    font-weight: var(--font-weight-medium);
    padding: var(--space-0-5) var(--space-1-5);
    font-size: inherit;
    min-width: 0;
    flex: 1;
    box-sizing: border-box;
  }

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
    background: currentColor;
    background: color-mix(in srgb, currentColor 12%, transparent);
    white-space: nowrap;
  }

  .gender-chip {
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    padding: 3px var(--space-2);
    border-radius: var(--radius-xl);
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 3px;
    line-height: 1;
    vertical-align: middle;
  }

  .gender-coherent {
    background: color-mix(in srgb, var(--success-color) 12%, transparent);
    color: var(--success-color);
  }

  .gender-conflict {
    background: color-mix(in srgb, var(--warning-color) 12%, transparent);
    color: var(--warning-color);
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
    color: var(--success-color);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 150px;
  }

  .card-body {
    border-top: 1px solid var(--border-color);
    padding: var(--space-3) var(--space-4);
  }

  .card-actions {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
    margin-bottom: var(--space-3);
    padding-bottom: var(--space-3);
    border-bottom: 1px solid var(--border-color);
  }

  .action-btn {
    padding: var(--space-1) var(--space-3);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-color);
    background: transparent;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    cursor: pointer;
    transition: all var(--duration-fast) ease;
  }

  .action-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    pointer-events: none;
  }

  /* Default (uncolored) hover — grey fill */
  .action-btn:hover {
    background: var(--button-hover);
    color: var(--text-color);
    transform: scale(1.02);
  }

  /* Merge — blue outline → solid blue */
  .action-btn.merge {
    color: var(--primary-on-surface);
    border-color: #3b82f6;
  }

  .action-btn.merge:hover {
    background: var(--primary-color);
    border-color: #3b82f6;
    color: var(--text-on-solid);
    transform: scale(1.02);
    box-shadow: 0 3px 8px rgba(var(--accent-rgb), 0.3);
  }

  /* Split — blue outline → solid blue (matches primary action palette) */
  .action-btn.split {
    color: var(--primary-on-surface);
    border-color: var(--primary-color);
  }

  .action-btn.split:hover {
    background: var(--primary-hover);
    border-color: var(--primary-hover);
    color: var(--text-on-solid);
    transform: scale(1.02);
    box-shadow: 0 3px 8px rgba(var(--accent-rgb), 0.3);
  }

  /* Promote — blue outline → solid blue */
  .action-btn.promote {
    color: var(--primary-on-surface);
    border-color: #3b82f6;
  }

  .action-btn.promote:hover {
    background: var(--primary-color);
    border-color: var(--primary-color);
    color: var(--text-on-solid);
    transform: scale(1.02);
    box-shadow: 0 3px 8px rgba(var(--accent-rgb), 0.3);
  }

  /* Unassign — amber outline → solid amber */
  .action-btn.unassign {
    color: #f59e0b;
    border-color: #f59e0b;
  }

  .action-btn.unassign:hover {
    background: #d97706;
    border-color: #d97706;
    color: var(--text-on-solid);
    transform: scale(1.02);
    box-shadow: 0 3px 8px rgba(217, 119, 6, 0.3);
  }

  /* Delete — red outline → solid red */
  .action-btn.delete {
    color: #ef4444;
    border-color: #ef4444;
  }

  .action-btn.delete:hover {
    background: var(--error-solid);
    border-color: var(--error-solid);
    color: var(--text-on-solid);
    transform: scale(1.02);
    box-shadow: 0 3px 8px rgba(220, 38, 38, 0.3);
  }

  /* Confirm Unassign — solid red (irreversible action, stays solid) */
  .action-btn.confirm-unassign {
    background: #ef4444;
    color: var(--text-on-solid);
    border-color: #ef4444;
  }

  .action-btn.confirm-unassign:hover:not(:disabled) {
    background: var(--error-solid);
    border-color: var(--error-solid);
    color: var(--text-on-solid);
    transform: scale(1.02);
    box-shadow: 0 3px 8px rgba(220, 38, 38, 0.3);
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
    color: var(--warning-color);
    white-space: nowrap;
  }

  .selection-chip {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .blacklist-toggle {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    font-size: var(--font-size-xs);
    color: var(--text-color);
    cursor: pointer;
    white-space: nowrap;
  }

  .blacklist-toggle input[type="checkbox"] {
    accent-color: var(--error-color);
    width: 14px;
    height: 14px;
  }

  .skeleton-members {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .skeleton-row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) 0;
  }

  .skeleton-avatar {
    width: 32px;
    height: 32px;
    border-radius: var(--radius-round);
    background: var(--border-color);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-text {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .skeleton-line {
    height: 12px;
    border-radius: var(--radius-sm);
    background: var(--border-color);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-line.short {
    width: 60%;
  }

  .cluster-avatar {
    width: 24px;
    height: 24px;
    border-radius: var(--radius-round);
    object-fit: cover;
    flex-shrink: 0;
  }

  @media (max-width: 768px) {
    .card-header {
      padding: var(--space-2-5) var(--space-3);
      flex-wrap: wrap;
    }

    .header-left {
      flex: 1 1 auto;
      min-width: 0;
    }

    .header-right {
      flex-shrink: 1;
      min-width: 0;
      gap: var(--space-1-5);
    }

    .cluster-label {
      font-size: var(--font-size-md);
    }

    .quality-badge,
    .gender-chip,
    .outlier-chip {
      font-size: 10px;
      padding: 1px var(--space-1-5);
    }

    .member-count {
      font-size: var(--font-size-xs);
    }

    .promoted-badge {
      max-width: 100px;
      font-size: 10px;
      padding: 1px var(--space-1-5);
    }

    .card-body {
      padding: var(--space-2-5) var(--space-3);
    }

    .card-actions {
      gap: var(--space-1-5);
      margin-bottom: var(--space-2);
      padding-bottom: var(--space-2);
    }

    .action-btn {
      padding: var(--space-1) var(--space-2);
      font-size: var(--font-size-xs);
    }

    .selection-chip {
      font-size: var(--font-size-2xs);
    }

    .blacklist-toggle {
      font-size: var(--font-size-2xs);
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
