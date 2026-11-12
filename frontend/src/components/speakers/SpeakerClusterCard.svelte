<script lang="ts">
  import { focusOnMount } from '$lib/actions/focusOnMount';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { createEventDispatcher, tick } from 'svelte';
  import type { SpeakerCluster } from '$lib/types/speakerCluster';
  import { t } from '$stores/locale';
  import ClusterHeaderChips from './ClusterHeaderChips.svelte';

  export let cluster: SpeakerCluster;
  export let expanded = false;
  export let actionInProgress = false;
  export let loading = false;
  export let unassignActive = false;
  export let unassignSelectedCount = 0;
  export let unassignTotalCount = 0;
  export let unassignBlacklist = true;

  const dispatch = createEventDispatcher();

  $: bodyId = `cluster-body-${cluster.uuid}`;

  let editingLabel = false;
  let labelInput = cluster.label || '';
  // An edit opened from the keyboard and ended with Enter or Escape hands focus back to the edit
  // control, so the keyboard user is not dropped onto <body>. A double-click edit, or one ended
  // by clicking elsewhere (blur), leaves focus where the user put it.
  let editFromKeyboard = false;
  let editButton: HTMLButtonElement | undefined;

  function toggleExpand() {
    expanded = !expanded;
    if (expanded) {
      dispatch('expand', { uuid: cluster.uuid });
    }
  }

  function startEdit(fromKeyboard = false) {
    labelInput = cluster.label || '';
    editFromKeyboard = fromKeyboard;
    editingLabel = true;
  }

  /** Double-clicking the cluster's name edits it, as before; a double-click elsewhere on the row does not. */
  function editOnLabelDblclick(event: MouseEvent) {
    if ((event.target as Element | null)?.closest('.cluster-label')) startEdit();
  }

  async function endEdit(byKey: boolean) {
    editingLabel = false;
    const restoreFocus = byKey && editFromKeyboard;
    editFromKeyboard = false;
    if (!restoreFocus) return;
    await tick();
    editButton?.focus();
  }

  function saveLabel(byKey = false) {
    if (!editingLabel) return;
    const normalizedLabel = labelInput.trim() || null;
    if (normalizedLabel !== cluster.label) {
      dispatch('update', { uuid: cluster.uuid, label: normalizedLabel });
    }
    void endEdit(byKey);
  }

  function cancelEdit() {
    labelInput = cluster.label || '';
    void endEdit(true);
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') saveLabel(true);
    if (e.key === 'Escape') cancelEdit();
  }
</script>

<div class="cluster-card" class:expanded>
  <!--
    WAI-ARIA APG disclosure pattern: the row is a real <button aria-expanded> (Enter and Space
    toggle it natively). It holds only text, because a button's children are presentational
    (axe nested-interactive): the label editor is a separate control beside it, never inside.
  -->
  <div class="card-header">
    {#if editingLabel}
      <div class="header-row">
        <span class="header-left">
          <span class="expand-icon" aria-hidden="true"><MenuIcon name={expanded ? 'chevron-down' : 'chevron-right'} size="xs" /></span>
          {#if cluster.promoted_to_profile_avatar_url}
            <img class="cluster-avatar" src={cluster.promoted_to_profile_avatar_url} alt="" loading="lazy" decoding="async" />
          {/if}
          <input
            class="label-input"
            bind:value={labelInput}
            on:blur={() => saveLabel()}
            on:keydown={handleKeydown}
            use:focusOnMount
            maxlength="200"
            placeholder={$t('speakers.cluster.editLabel')}
            aria-label={$t('speakers.cluster.editLabel')}
          />
        </span>
        <ClusterHeaderChips {cluster} />
      </div>
    {:else}
      <button
        type="button"
        class="header-row card-toggle"
        aria-expanded={expanded}
        aria-controls={expanded ? bodyId : undefined}
        title={$t('speakers.tooltip.expandCluster')}
        on:click={toggleExpand}
        on:dblclick={editOnLabelDblclick}
      >
        <span class="header-left">
          <span class="expand-icon" aria-hidden="true"><MenuIcon name={expanded ? 'chevron-down' : 'chevron-right'} size="xs" /></span>
          {#if cluster.promoted_to_profile_avatar_url}
            <img class="cluster-avatar" src={cluster.promoted_to_profile_avatar_url} alt="" loading="lazy" decoding="async" />
          {/if}
          {#if cluster.label || cluster.promoted_to_profile_name}
            <span class="cluster-label" title={$t('speakers.tooltip.editLabel')}>
              {cluster.label || cluster.promoted_to_profile_name}
            </span>
          {:else if cluster.suggested_name}
            <span class="cluster-label suggested" title={$t('speakers.tooltip.suggestedName')}>
              {cluster.suggested_name}
            </span>
            <span class="suggested-badge" title={$t('speakers.tooltip.suggestedName')}>{$t('speakers.cluster.suggested')}</span>
          {:else}
            <span class="cluster-label" title={$t('speakers.tooltip.editLabel')}>
              {$t('speakers.cluster.unlabeled')}
            </span>
          {/if}
        </span>
        <ClusterHeaderChips {cluster} />
      </button>
      <!-- Hidden until focused, like a skip link: the keyboard's way into the label editor
           (the double-click on the name is the pointer's). -->
      <button type="button" class="sr-only edit-label-btn" bind:this={editButton} on:click={() => startEdit(true)}>
        {$t('speakers.cluster.editLabelButton')}
      </button>
    {/if}
  </div>

  {#if expanded}
    <div class="card-body" id={bodyId}>
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
    min-width: 0;
  }

  /* The disclosure button and the label-editing row share one layout. */
  .header-row {
    display: flex;
    flex: 1;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-3) var(--space-4);
    gap: var(--space-2);
    min-width: 0;
  }

  /* Undo the global <button> look (form-elements.css): the row looks like the card, not a button. */
  .card-toggle {
    margin: 0;
    border: 0;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
    color: inherit;
    font: inherit;
    text-align: start;
    cursor: pointer;
    user-select: none;
    transition: none;
  }

  .card-toggle:hover {
    background: var(--hover-color);
    box-shadow: none;
  }

  .card-toggle:focus-visible {
    outline-offset: -2px;
  }

  /* `.sr-only` until it has focus; then a small text button at the end of the row. */
  .edit-label-btn:focus {
    position: static;
    width: auto;
    height: auto;
    margin: auto var(--space-2);
    padding: var(--space-0-5) var(--space-2);
    overflow: visible;
    clip-path: none;
    font-size: var(--font-size-sm);
    box-shadow: none;
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
    font-size: var(--font-size-2xs);
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
    border-color: var(--info-color);
  }

  .action-btn.merge:hover {
    background: var(--primary-color);
    border-color: var(--primary-color);
    color: var(--text-on-solid);
    transform: scale(1.02);
    box-shadow: var(--shadow-primary-hover);
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
    box-shadow: var(--shadow-primary-hover);
  }

  /* Promote — blue outline → solid blue */
  .action-btn.promote {
    color: var(--primary-on-surface);
    border-color: var(--info-color);
  }

  .action-btn.promote:hover {
    background: var(--primary-color);
    border-color: var(--primary-color);
    color: var(--text-on-solid);
    transform: scale(1.02);
    box-shadow: var(--shadow-primary-hover);
  }

  /* Unassign — amber outline → solid amber */
  .action-btn.unassign {
    color: var(--warning-text);
    border-color: var(--warning-color);
  }

  .action-btn.unassign:hover {
    background: var(--warning-solid-hover);
    border-color: var(--warning-solid-hover);
    color: var(--text-on-warning);
    transform: scale(1.02);
    box-shadow: var(--shadow-hover);
  }

  /* Delete — red outline → solid red */
  .action-btn.delete {
    color: var(--error-text);
    border-color: var(--error-color);
  }

  .action-btn.delete:hover {
    background: var(--error-solid);
    border-color: var(--error-solid);
    color: var(--text-on-solid);
    transform: scale(1.02);
    box-shadow: var(--shadow-danger-hover);
  }

  /* Confirm Unassign — solid red (irreversible action, stays solid) */
  .action-btn.confirm-unassign {
    background: var(--error-solid);
    color: var(--text-on-solid);
    border-color: var(--error-solid);
  }

  .action-btn.confirm-unassign:hover:not(:disabled) {
    background: var(--error-solid);
    border-color: var(--error-solid);
    color: var(--text-on-solid);
    transform: scale(1.02);
    box-shadow: var(--shadow-danger-hover);
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
    .header-row {
      padding: var(--space-2-5) var(--space-3);
      flex-wrap: wrap;
    }

    .header-left {
      flex: 1 1 auto;
      min-width: 0;
    }

    .cluster-label {
      font-size: var(--font-size-md);
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
</style>
