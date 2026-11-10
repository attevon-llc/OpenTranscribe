<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { SpeakerCluster, SpeakerClusterMember } from '$lib/types/speakerCluster';
  import type { OutlierAnalysisResponse, MinorityAnalysisItem } from '$lib/types/speakerCluster';
  import { analyzeClusterOutliers } from '$lib/api/speakerClusters';
  import { audioPlaybackStore } from '$stores/audioPlaybackStore';
  import { t } from '$stores/locale';
  import GenderBadge from './GenderBadge.svelte';

  export let members: SpeakerClusterMember[];
  export let cluster: SpeakerCluster;
  export let splitMode = false;
  export let splitTargetUuid: string | null = null;
  export let splitSelectedMembers: Set<string> = new Set();
  export let unassignMode = false;
  export let unassignTargetUuid: string | null = null;
  export let unassignSelectedMembers: Set<string> = new Set();

  const dispatch = createEventDispatcher();

  // Outlier analysis state
  let outlierAnalysis: OutlierAnalysisResponse | null = null;
  let outlierLoading = false;
  let outlierError = false;

  // Build a map of speaker_uuid -> analysis item for inline badges
  $: outlierMap = new Map<string, MinorityAnalysisItem>(
    (outlierAnalysis?.minority_analysis ?? []).map((item) => [item.speaker_uuid, item])
  );

  // Gender grouping: split members into majority and minority groups
  $: hasGenderConflict = cluster.gender_composition?.has_gender_conflict ?? false;
  $: dominantGender = cluster.gender_composition?.dominant_gender ?? null;

  $: majorityMembers = hasGenderConflict && dominantGender
    ? members.filter(
        (m) => !m.predicted_gender || m.predicted_gender === dominantGender
      )
    : members;

  $: minorityMembers = hasGenderConflict && dominantGender
    ? members.filter(
        (m) => m.predicted_gender != null && m.predicted_gender !== dominantGender
      )
    : [];

  // Check if this cluster is the active split/unassign target
  $: isSplitTarget = splitMode && splitTargetUuid === cluster.uuid;
  $: isUnassignTarget = unassignMode && unassignTargetUuid === cluster.uuid;
  $: isSelectable = isSplitTarget || isUnassignTarget;

  // Auto-fetch outlier analysis when cluster with gender conflict is expanded
  function maybeFetchOutlierAnalysis(conflict: boolean, memberCount: number) {
    if (conflict && memberCount > 0 && !outlierAnalysis && !outlierLoading && !outlierError) {
      void fetchOutlierAnalysis();
    }
  }

  $: maybeFetchOutlierAnalysis(hasGenderConflict, members.length);

  async function fetchOutlierAnalysis() {
    outlierLoading = true;
    outlierError = false;
    try {
      outlierAnalysis = await analyzeClusterOutliers(cluster.uuid);
    } catch {
      outlierError = true;
    } finally {
      outlierLoading = false;
    }
  }

  function handleToggleMember(speakerUuid: string) {
    if (isSplitTarget) {
      dispatch('toggleSplitMember', speakerUuid);
    } else if (isUnassignTarget) {
      dispatch('toggleUnassignMember', speakerUuid);
    }
  }

  function isChecked(speakerUuid: string): boolean {
    if (isSplitTarget) return splitSelectedMembers.has(speakerUuid);
    if (isUnassignTarget) return unassignSelectedMembers.has(speakerUuid);
    return false;
  }

  function badgeClass(recommendation: string): string {
    if (recommendation === 'likely_outlier') return 'outlier-badge-red';
    if (recommendation === 'borderline') return 'outlier-badge-yellow';
    return 'outlier-badge-green';
  }

  function badgeLabel(item: MinorityAnalysisItem): string {
    const sim = (item.sim_to_centroid * 100).toFixed(0);
    if (item.recommendation === 'likely_outlier') return `Outlier ${sim}%`;
    if (item.recommendation === 'borderline') return `Borderline ${sim}%`;
    return `Valid ${sim}%`;
  }

  // Notify parent when outlier analysis completes (for pre-selecting outliers in unassign)
  $: if (outlierAnalysis) {
    dispatch('outlierAnalysisComplete', {
      clusterUuid: cluster.uuid,
      outlierUuids: (outlierAnalysis.minority_analysis ?? [])
        .filter((a) => a.recommendation === 'likely_outlier')
        .map((a) => a.speaker_uuid),
    });
  }
</script>

{#if outlierLoading}
  <div class="outlier-loading">{$t('speakers.cluster.analyzingOutliers')}</div>
{/if}

<!-- Action buttons at TOP for split mode -->
{#if isSplitTarget}
  <div class="mode-banner">
    <span>{$t('speakers.split.selectMembersCount', { selected: splitSelectedMembers.size, total: members.length })}</span>
  </div>
  <div class="mode-actions">
    <button class="btn-cancel" on:click={() => dispatch('cancelSplit')}>{$t('modal.cancel')}</button>
    <button class="btn-confirm" on:click={() => dispatch('confirmSplit')} disabled={splitSelectedMembers.size === 0 || splitSelectedMembers.size === members.length}>{$t('speakers.split.confirm', { count: splitSelectedMembers.size })}</button>
  </div>
{/if}

{#snippet memberRow(member: SpeakerClusterMember, showOutlier: boolean)}
    {#if isSelectable}
      <input type="checkbox" checked={isChecked(member.speaker_uuid)} on:change={() => handleToggleMember(member.speaker_uuid)} />
    {/if}
    {#if member.has_audio_clip}
      <button
        class="member-play-btn"
        class:playing={$audioPlaybackStore.activeSpeakerUuid === member.speaker_uuid && $audioPlaybackStore.isPlaying}
        on:click|stopPropagation={() => dispatch('preview', { speaker_uuid: member.speaker_uuid })}
        on:mouseenter={() => dispatch('prefetch', { speaker_uuid: member.speaker_uuid })}
        title={$t('speakers.audioClip.play')}
      >
        {#if $audioPlaybackStore.activeSpeakerUuid === member.speaker_uuid && $audioPlaybackStore.isPlaying}
          <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="2" width="4" height="12" rx="1" /><rect x="9" y="2" width="4" height="12" rx="1" /></svg>
        {:else}
          <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M4 2l10 6-10 6V2z" /></svg>
        {/if}
      </button>
    {/if}
    <span class="member-name">{member.display_name || member.speaker_name}</span>
    <span class="member-file">{member.media_file_title || ''}</span>
    <span class="member-confidence">{member.confidence != null && !isNaN(member.confidence) ? (member.confidence * 100).toFixed(0) + '%' : '\u2014'}</span>
    <GenderBadge
      gender={member.predicted_gender}
      confidence={member.gender_confidence}
      confirmed={member.gender_confirmed_by_user}
    />
      {#if showOutlier && outlierMap.has(member.speaker_uuid)}
        {@const analysis = outlierMap.get(member.speaker_uuid)}
        {#if analysis}
          <span class="outlier-badge {badgeClass(analysis.recommendation)}" title={`Centroid sim: ${(analysis.sim_to_centroid * 100).toFixed(0)}%, Avg to majority: ${(analysis.avg_sim_to_majority * 100).toFixed(0)}%`}>
            {badgeLabel(analysis)}
          </span>
        {/if}
      {/if}
    {#if member.verified}<span class="verified-badge">{$t('speakers.verified')}</span>{/if}
{/snippet}


<!-- Member list -->
<div class="member-list">
  {#if minorityMembers.length > 0}
    <div class="gender-separator">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
        <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
      </svg>
      <span>{$t('speakers.cluster.genderMismatchGroup', { count: minorityMembers.length })}</span>
      <span class="separator-hint" title={$t('speakers.cluster.genderMismatchHint')}>{$t('speakers.cluster.genderMismatchHint')}</span>
    </div>

    {#each minorityMembers as member (member.speaker_uuid)}
      <div
        class="member-row gender-outlier"
        class:split-selectable={isSelectable}
      >
        {@render memberRow(member, true)}
      </div>
    {/each}

    <div class="majority-separator">
      <span>{$t('speakers.cluster.memberCount', { count: majorityMembers.length })}</span>
    </div>
  {/if}

  {#each majorityMembers as member (member.speaker_uuid)}
    <div
      class="member-row"
      class:split-selectable={isSelectable}
    >
      {@render memberRow(member, false)}
    </div>
  {/each}
</div>

<style>
  .member-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .member-row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-1-5) var(--space-2);
    border-radius: var(--radius-sm);
    font-size: 13px;
    min-width: 0;
  }

  .member-row:hover {
    background: var(--hover-color);
  }

  .member-name {
    font-weight: 500;
    color: var(--text-color);
    min-width: 80px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex-shrink: 0;
    max-width: 200px;
  }

  .member-file {
    color: var(--text-secondary);
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .member-confidence {
    color: var(--text-secondary);
    font-size: 12px;
  }

  .verified-badge {
    font-size: 11px;
    padding: 1px var(--space-1-5);
    border-radius: var(--radius-lg);
    background: color-mix(in srgb, var(--success-color) 15%, transparent);
    color: var(--success-color);
  }

  /* .gender-icon / .gender-svg / .gender-confirmed-tick moved to GenderBadge.svelte
     (issue #756) — Svelte scopes <style> per component, so markup rendered by a child
     needs its styles to live there, not here. */

  .gender-outlier {
    background: color-mix(in srgb, var(--warning-color) 8%, transparent);
  }

  .split-selectable {
    cursor: pointer;
  }

  .split-selectable:hover {
    background: var(--hover-color);
  }

  .split-selectable input[type="checkbox"] {
    accent-color: var(--primary-color);
    width: 16px;
    height: 16px;
    cursor: pointer;
  }

  .member-play-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    border-radius: var(--radius-round);
    border: 1px solid var(--border-color);
    background: var(--card-background);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all var(--duration-fast) ease;
    flex-shrink: 0;
    box-shadow: none;
    font-size: 0;
  }

  .member-play-btn:hover {
    background: var(--primary-color);
    color: var(--text-on-solid);
    border-color: var(--primary-color);
    transform: none;
    box-shadow: none;
  }

  .member-play-btn.playing {
    background: var(--primary-color);
    color: var(--text-on-solid);
    border-color: var(--primary-color);
  }

  /* Gender separator */
  .gender-separator {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-2) var(--space-1);
    margin-top: var(--space-2);
    border-top: 1px dashed var(--warning-color);
    font-size: 12px;
    font-weight: 600;
    color: var(--warning-color);
    flex-wrap: wrap;
    min-width: 0;
  }

  .separator-hint {
    font-weight: 400;
    font-size: 11px;
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }

  .majority-separator {
    padding: var(--space-1-5) var(--space-2) var(--space-0-5);
    margin-top: var(--space-1);
    border-top: 1px solid var(--border-color);
    font-size: 12px;
    color: var(--text-secondary);
  }

  .outlier-loading {
    padding: var(--space-1-5) var(--space-2);
    font-size: 12px;
    color: var(--text-secondary);
    font-style: italic;
  }

  /* Outlier badges */
  .outlier-badge {
    font-size: 10px;
    font-weight: 600;
    padding: 1px var(--space-1-5);
    border-radius: var(--radius-lg);
    white-space: nowrap;
  }

  .outlier-badge-red {
    background: color-mix(in srgb, var(--error-color) 15%, transparent);
    color: var(--error-color);
  }

  .outlier-badge-yellow {
    background: color-mix(in srgb, var(--warning-color) 15%, transparent);
    color: var(--warning-color);
  }

  .outlier-badge-green {
    background: color-mix(in srgb, var(--success-color) 15%, transparent);
    color: var(--success-color);
  }

  /* Mode banners and actions */
  .mode-banner {
    padding: var(--space-2) var(--space-3);
    background: var(--color-warning-bg);
    border: 1px solid var(--color-warning-border);
    border-radius: var(--radius-md);
    margin-bottom: var(--space-2);
    font-size: 13px;
    color: var(--warning-color);
  }

  .mode-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding-bottom: var(--space-2);
    margin-bottom: var(--space-2);
    border-bottom: 1px solid var(--border-color);
  }

  .btn-cancel {
    padding: var(--space-1-5) var(--space-3-5);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background: var(--card-background);
    color: var(--text-color);
    cursor: pointer;
    font-size: 13px;
    box-shadow: none;
  }

  .btn-cancel:hover {
    background: var(--hover-color);
    transform: none;
    box-shadow: none;
  }

  .btn-confirm {
    padding: var(--space-1-5) var(--space-3-5);
    border: none;
    border-radius: var(--radius-lg);
    background: var(--primary-color);
    color: var(--text-on-solid);
    cursor: pointer;
    font-size: 13px;
    box-shadow: none;
  }

  .btn-confirm:hover:not(:disabled) {
    opacity: 0.9;
    transform: none;
    box-shadow: none;
  }

  .btn-confirm:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  @media (max-width: 768px) {
    .member-row {
      gap: var(--space-1-5);
      padding: var(--space-1-5) var(--space-1);
      font-size: 12px;
    }

    .member-name {
      min-width: 60px;
      max-width: 120px;
      font-size: 12px;
    }

    .member-confidence {
      font-size: 11px;
    }

    .outlier-badge {
      font-size: 9px;
      padding: 1px var(--space-1);
    }

    .verified-badge {
      font-size: 10px;
      padding: 1px var(--space-1);
    }

    .mode-banner {
      padding: var(--space-1-5) var(--space-2);
      font-size: 12px;
    }

    .mode-actions {
      gap: var(--space-1-5);
    }

    .btn-cancel,
    .btn-confirm {
      padding: 5px var(--space-2-5);
      font-size: 12px;
    }

    .gender-separator {
      font-size: 11px;
      gap: var(--space-1);
    }

    .separator-hint {
      font-size: 10px;
    }
  }

  @media (max-width: 480px) {
    .member-name {
      min-width: 40px;
      max-width: 90px;
    }

    .member-file {
      display: none;
    }
  }
</style>
