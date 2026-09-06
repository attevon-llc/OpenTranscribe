<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { SpeakerCluster } from '$lib/types/speakerCluster';
  import { t } from '$stores/locale';

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

  function qualityColor(score: number | null): string {
    if (score === null) return 'var(--text-secondary)';
    if (score >= 0.8) return 'var(--success-color, #10b981)';
    if (score >= 0.6) return 'var(--warning-color, #f59e0b)';
    return 'var(--error-color, #ef4444)';
  }
</script>

<div class="cluster-card" class:expanded>
  <div class="card-header" on:click={toggleExpand} on:keydown={toggleExpand} role="button" tabindex="0" title={$t('speakers.tooltip.expandCluster')}>
    <div class="header-left">
      <span class="expand-icon">{expanded ? '▾' : '▸'}</span>
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
        <!-- svelte-ignore a11y-autofocus -->
        <input
          class="label-input"
          bind:value={labelInput}
          on:blur={saveLabel}
          on:keydown={handleKeydown}
          on:click|stopPropagation
          autofocus
          maxlength="200"
          placeholder={$t('speakers.cluster.editLabel')}
        />
      {:else}
        <!-- svelte-ignore a11y-no-static-element-interactions -->
        {#if cluster.label || cluster.promoted_to_profile_name}
          <span class="cluster-label" on:dblclick|stopPropagation={startEdit} title={$t('speakers.tooltip.editLabel')}>
            {cluster.label || cluster.promoted_to_profile_name}
          </span>
        {:else if cluster.suggested_name}
          <span class="cluster-label suggested" on:dblclick|stopPropagation={startEdit} title={$t('speakers.tooltip.suggestedName')}>
            {cluster.suggested_name}
          </span>
          <span class="suggested-badge" title={$t('speakers.tooltip.suggestedName')}>{$t('speakers.cluster.suggested')}</span>
        {:else}
          <span class="cluster-label" on:dblclick|stopPropagation={startEdit} title={$t('speakers.tooltip.editLabel')}>
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
      {#if cluster.gender_composition && cluster.gender_composition.total_with_gender > 0}
        <span
          class="gender-chip"
          class:gender-coherent={!cluster.gender_composition.has_gender_conflict}
          class:gender-conflict={cluster.gender_composition.has_gender_conflict}
          title={cluster.gender_composition.has_gender_conflict ? $t('speakers.cluster.genderConflict') : $t('speakers.cluster.genderCoherent')}
        >
          {#if cluster.gender_composition.dominant_gender === 'male'}
            <svg class="gender-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="14" r="7"/><line x1="15" y1="9" x2="21" y2="3"/><polyline points="15 3 21 3 21 9"/></svg>
          {:else}
            <svg class="gender-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="9" r="7"/><line x1="12" y1="16" x2="12" y2="23"/><line x1="9" y1="20" x2="15" y2="20"/></svg>
          {/if}
          {cluster.gender_composition.gender_label}
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
          {#each Array(Math.min(cluster.member_count, 3)) as _}
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
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 8px;
    background: var(--card-background, #ffffff);
    overflow: hidden;
    transition: box-shadow 0.15s ease;
  }

  .cluster-card:hover {
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
  }

  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    cursor: pointer;
    user-select: none;
    gap: 8px;
    min-width: 0;
  }

  .card-header:hover {
    background: var(--hover-color, #f9fafb);
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
    min-width: 0;
  }

  .expand-icon {
    color: var(--text-secondary, #9ca3af);
    font-size: 12px;
    width: 16px;
  }

  .cluster-label {
    font-weight: 500;
    color: var(--text-color, #111827);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cluster-label.suggested {
    font-style: italic;
    color: var(--primary-color, #3b82f6);
  }

  .suggested-badge {
    font-size: 10px;
    font-weight: 600;
    padding: 1px 6px;
    border-radius: 8px;
    background: color-mix(in srgb, var(--primary-color, var(--primary-color)) 12%, transparent);
    color: var(--primary-color, #3b82f6);
    white-space: nowrap;
    flex-shrink: 0;
  }

  .label-input {
    font-weight: 500;
    border: 1px solid var(--primary-color, #3b82f6);
    border-radius: 4px;
    padding: 2px 6px;
    font-size: inherit;
    background: var(--card-background, #ffffff);
    color: var(--text-color, #111827);
    outline: none;
    min-width: 0;
    flex: 1;
    box-sizing: border-box;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .quality-badge {
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 10px;
    background: currentColor;
    background: color-mix(in srgb, currentColor 12%, transparent);
    white-space: nowrap;
  }

  .gender-chip {
    font-size: 11px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 10px;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 3px;
    line-height: 1;
    vertical-align: middle;
  }

  .gender-svg {
    width: 12px;
    height: 12px;
    flex-shrink: 0;
  }

  .gender-coherent {
    background: color-mix(in srgb, var(--success-color, #10b981) 12%, transparent);
    color: var(--success-color, #10b981);
  }

  .gender-conflict {
    background: color-mix(in srgb, var(--warning-color, #f59e0b) 12%, transparent);
    color: var(--warning-color, #f59e0b);
  }

  .member-count {
    font-size: 13px;
    color: var(--text-secondary, #6b7280);
    white-space: nowrap;
  }

  .promoted-badge {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    background: color-mix(in srgb, var(--success-color, #059669) 15%, transparent);
    color: var(--success-color, #059669);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 150px;
  }

  .card-body {
    border-top: 1px solid var(--border-color, #e5e7eb);
    padding: 12px 16px;
  }

  .card-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 12px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border-color, #e5e7eb);
  }

  .action-btn {
    padding: 4px 12px;
    border-radius: 6px;
    border: 1px solid var(--border-color, #d1d5db);
    background: transparent;
    color: var(--text-secondary, #6b7280);
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .action-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    pointer-events: none;
  }

  /* Default (uncolored) hover — grey fill */
  .action-btn:hover {
    background: var(--button-hover, #e5e7eb);
    color: var(--text-color, #374151);
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
    color: white;
    transform: scale(1.02);
    box-shadow: 0 3px 8px rgba(59, 130, 246, 0.3);
  }

  /* Split — blue outline → solid blue (matches primary action palette) */
  .action-btn.split {
    color: var(--primary-color, #3b82f6);
    border-color: var(--primary-color, #3b82f6);
  }

  .action-btn.split:hover {
    background: var(--primary-hover, #2563eb);
    border-color: var(--primary-hover, #2563eb);
    color: white;
    transform: scale(1.02);
    box-shadow: 0 3px 8px rgba(59, 130, 246, 0.3);
  }

  /* Promote — blue outline → solid blue */
  .action-btn.promote {
    color: var(--primary-on-surface);
    border-color: #3b82f6;
  }

  .action-btn.promote:hover {
    background: #2563eb;
    border-color: #2563eb;
    color: white;
    transform: scale(1.02);
    box-shadow: 0 3px 8px rgba(59, 130, 246, 0.3);
  }

  /* Unassign — amber outline → solid amber */
  .action-btn.unassign {
    color: #f59e0b;
    border-color: #f59e0b;
  }

  .action-btn.unassign:hover {
    background: #d97706;
    border-color: #d97706;
    color: white;
    transform: scale(1.02);
    box-shadow: 0 3px 8px rgba(217, 119, 6, 0.3);
  }

  /* Delete — red outline → solid red */
  .action-btn.delete {
    color: #ef4444;
    border-color: #ef4444;
  }

  .action-btn.delete:hover {
    background: #dc2626;
    border-color: #dc2626;
    color: white;
    transform: scale(1.02);
    box-shadow: 0 3px 8px rgba(220, 38, 38, 0.3);
  }

  /* Confirm Unassign — solid red (irreversible action, stays solid) */
  .action-btn.confirm-unassign {
    background: #ef4444;
    color: white;
    border-color: #ef4444;
  }

  .action-btn.confirm-unassign:hover:not(:disabled) {
    background: #dc2626;
    border-color: #dc2626;
    color: white;
    transform: scale(1.02);
    box-shadow: 0 3px 8px rgba(220, 38, 38, 0.3);
  }

  .outlier-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
    background: color-mix(in srgb, var(--warning-color, #f59e0b) 12%, transparent);
    color: var(--warning-color, #f59e0b);
    white-space: nowrap;
  }

  .selection-chip {
    font-size: 12px;
    color: var(--text-secondary, #6b7280);
    white-space: nowrap;
  }

  .blacklist-toggle {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    color: var(--text-color);
    cursor: pointer;
    white-space: nowrap;
  }

  .blacklist-toggle input[type="checkbox"] {
    accent-color: var(--error-color, #ef4444);
    width: 14px;
    height: 14px;
  }

  .skeleton-members {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .skeleton-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 0;
  }

  .skeleton-avatar {
    width: 32px;
    height: 32px;
    border-radius: 50%;
    background: var(--border-color, #e5e7eb);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-text {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .skeleton-line {
    height: 12px;
    border-radius: 4px;
    background: var(--border-color, #e5e7eb);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-line.short {
    width: 60%;
  }

  .cluster-avatar {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    object-fit: cover;
    flex-shrink: 0;
  }

  @media (max-width: 768px) {
    .card-header {
      padding: 10px 12px;
      flex-wrap: wrap;
    }

    .header-left {
      flex: 1 1 auto;
      min-width: 0;
    }

    .header-right {
      flex-shrink: 1;
      min-width: 0;
      gap: 6px;
    }

    .cluster-label {
      font-size: 14px;
    }

    .quality-badge,
    .gender-chip,
    .outlier-chip {
      font-size: 10px;
      padding: 1px 6px;
    }

    .member-count {
      font-size: 12px;
    }

    .promoted-badge {
      max-width: 100px;
      font-size: 10px;
      padding: 1px 6px;
    }

    .card-body {
      padding: 10px 12px;
    }

    .card-actions {
      gap: 6px;
      margin-bottom: 8px;
      padding-bottom: 8px;
    }

    .action-btn {
      padding: 4px 8px;
      font-size: 12px;
    }

    .selection-chip {
      font-size: 11px;
    }

    .blacklist-toggle {
      font-size: 11px;
    }
  }

  @media (max-width: 480px) {
    .header-right {
      flex-basis: 100%;
      justify-content: flex-start;
      margin-top: 4px;
    }

    .promoted-badge {
      max-width: 140px;
    }
  }
</style>
