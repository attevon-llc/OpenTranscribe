<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { SpeakerInboxItem as InboxItemType } from '$lib/types/speakerCluster';
  import { audioPlaybackStore } from '$stores/audioPlaybackStore';
  import { t } from '$stores/locale';
  import GenderBadge from './GenderBadge.svelte';

  export let item: InboxItemType;
  export let actionInProgress = false;
  /**
   * Existing profiles this speaker can be attached to.
   *
   * Without this the inbox was a dead end whenever the matcher produced no
   * `suggested_name`: the row rendered ONLY "Skip", so a speaker the user could
   * plainly identify had no route to a profile. That is the common case for a
   * voice that fluctuates and gets split across several detections — exactly the
   * situation the inbox exists to resolve.
   */
  export let profiles: Array<{ uuid: string; name: string }> = [];

  let assignTo = '';

  function assignToProfile() {
    if (!assignTo) return;
    dispatch('action', { type: 'assign', speaker_uuid: item.speaker_uuid, profile_uuid: assignTo });
    assignTo = '';
  }

  const dispatch = createEventDispatcher();

  function confidenceColor(confidence: number | null): string {
    if (confidence === null) return 'var(--text-secondary)';
    if (confidence >= 0.75) return 'var(--success-text)';
    if (confidence >= 0.5) return 'var(--warning-text)';
    return 'var(--error-text)';
  }

  function sourceLabel(source: string | null): string {
    if (!source) return '';
    const key = `speakers.inbox.source.${source}`;
    const translated = $t(key);
    return translated !== key ? translated : source.replace(/_/g, ' ');
  }

  function sourceClass(source: string | null): string {
    if (source === 'profile_match') return 'source-profile';
    if (source === 'voice_match') return 'source-voice';
    if (source === 'llm_analysis') return 'source-llm';
    return '';
  }
</script>

<div class="inbox-item">
  <div class="inbox-row">
    <div class="item-left">
      <button
        class="preview-toggle"
        class:playing={$audioPlaybackStore.activeSpeakerUuid === item.speaker_uuid && $audioPlaybackStore.isPlaying}
        on:click={() => dispatch('preview', { speaker_uuid: item.speaker_uuid })}
        on:mouseenter={() => dispatch('prefetch', { speaker_uuid: item.speaker_uuid })}
        title={$t('speakers.inbox.previewTitle')}
      >
        {#if $audioPlaybackStore.activeSpeakerUuid === item.speaker_uuid && $audioPlaybackStore.isPlaying}
          <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="currentColor" stroke="none">
            <rect x="6" y="4" width="4" height="16" rx="1" /><rect x="14" y="4" width="4" height="16" rx="1" />
          </svg>
        {:else}
          <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="currentColor" stroke="none">
            <path d="M8 5.14v13.72a1 1 0 0 0 1.5.86l11.04-6.86a1 1 0 0 0 0-1.72L9.5 4.28a1 1 0 0 0-1.5.86z"></path>
          </svg>
        {/if}
      </button>
      <div class="item-info">
        <div class="speaker-name">{item.speaker_name}</div>
        {#if item.suggested_name}
          <div class="suggestion">
            <span class="suggestion-label">{$t('speakers.inbox.suggestion')}</span>
            <span class="suggestion-name">{item.suggested_name}</span>
            {#if item.confidence != null && !isNaN(item.confidence)}
              <span class="confidence" style="color: {confidenceColor(item.confidence)}">
                <bdi dir="ltr">{(item.confidence * 100).toFixed(0)}%</bdi>
              </span>
            {/if}
            {#if item.suggestion_source}
              <span class="source-badge {sourceClass(item.suggestion_source)}">{sourceLabel(item.suggestion_source)}</span>
            {/if}
          </div>
        {/if}
        <div class="meta">
          {#if item.media_file_title}
            <span class="file-title">{item.media_file_title}</span>
          {/if}
          {#if item.predicted_gender}
            <span class="attribute"><GenderBadge gender={item.predicted_gender} /></span>
          {/if}
          {#if item.cluster_label}
            <span class="cluster-tag">{$t('speakers.inbox.clusterLabel')} {item.cluster_label}</span>
          {:else if item.cluster_member_count > 1}
            <span class="cluster-tag">{item.cluster_member_count} {$t('speakers.inbox.similar')}</span>
          {/if}
        </div>
      </div>
    </div>
    <div class="item-actions">
      {#if item.suggested_name}
        <button
          class="action-btn accept"
          disabled={actionInProgress}
          on:click={() => dispatch('action', { type: 'accept', speaker_uuid: item.speaker_uuid })}
          title={$t('speakers.inbox.acceptTitle')}
        >
          {$t('speakers.inbox.accept')}
        </button>
      {/if}
      {#if profiles.length > 0}
        <!-- The backend and API client already supported `assign` + profile_uuid;
             only this control was missing. -->
        <select
          class="assign-select"
          bind:value={assignTo}
          disabled={actionInProgress}
          on:change={assignToProfile}
          aria-label={$t('speakers.inbox.assignToProfile')}
          title={$t('speakers.inbox.assignToProfile')}
        >
          <option value="">{$t('speakers.inbox.assignToProfile')}</option>
          {#each profiles as profile (profile.uuid)}
            <option value={profile.uuid}>{profile.name}</option>
          {/each}
        </select>
      {/if}
      <button
        class="action-btn skip"
        disabled={actionInProgress}
        on:click={() => dispatch('action', { type: 'skip', speaker_uuid: item.speaker_uuid })}
        title={$t('speakers.inbox.skipTitle')}
      >
        {$t('speakers.inbox.skip')}
      </button>
    </div>
  </div>
</div>

<style>
  .inbox-item {
    border-bottom: 1px solid var(--border-color);
    transition: background 0.1s ease;
  }

  .inbox-item:hover {
    background: var(--hover-color);
  }

  .inbox-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-2-5) var(--space-4);
  }

  .item-left {
    display: flex;
    align-items: center;
    gap: var(--space-2-5);
    flex: 1;
    min-width: 0;
  }

  .preview-toggle {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    padding: 0;
    border: none;
    border-radius: var(--radius-md);
    background: none;
    color: var(--primary-on-surface);
    cursor: pointer;
    transition: all var(--duration-fast);
    box-shadow: none;
  }

  .preview-toggle:hover {
    background: color-mix(in srgb, var(--primary-color) 10%, transparent);
    transform: scale(1.1);
    box-shadow: none;
  }

  .preview-toggle.playing {
    color: var(--primary-on-surface);
    background: color-mix(in srgb, var(--primary-color) 12%, transparent);
  }

  .item-info {
    flex: 1;
    min-width: 0;
  }

  .speaker-name {
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    font-size: var(--font-size-md);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .suggestion {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    margin-top: var(--space-0-5);
    font-size: var(--font-size-sm);
    flex-wrap: wrap;
    min-width: 0;
  }

  .suggestion-label {
    color: var(--text-secondary);
  }

  .suggestion-name {
    color: var(--primary-on-surface);
    font-weight: var(--font-weight-medium);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 200px;
  }

  .confidence {
    font-weight: var(--font-weight-semibold);
    font-size: var(--font-size-xs);
  }

  .source-badge {
    font-size: var(--font-size-2xs);
    padding: 1px var(--space-1-5);
    border-radius: var(--radius-lg);
    background: var(--hover-color);
    color: var(--text-secondary);
  }

  .source-badge.source-profile {
    background: color-mix(in srgb, var(--success-color) 10%, transparent);
    color: var(--success-text);
  }

  .source-badge.source-voice {
    background: color-mix(in srgb, var(--primary-color) 10%, transparent);
    color: var(--primary-on-surface);
  }

  .source-badge.source-llm {
    background: color-mix(in srgb, var(--text-secondary) 10%, transparent);
    color: var(--text-secondary);
  }

  .meta {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-top: 3px;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    flex-wrap: wrap;
    min-width: 0;
  }

  .file-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 200px;
  }

  .attribute {
    text-transform: capitalize;
  }

  .cluster-tag {
    padding: 1px var(--space-1-5);
    border-radius: var(--radius-lg);
    background: var(--hover-color);
  }

  .item-actions {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    flex-shrink: 0;
  }

  .action-btn {
    /* Never let a one-word action wrap. Adding the assign dropdown to this row
       squeezed Skip until it broke across two lines as "Ski"/"p". */
    white-space: nowrap;
    padding: var(--space-1) var(--space-3);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-color);
    background: var(--hover-color);
    color: var(--text-color);
    font-size: var(--font-size-sm);
    cursor: pointer;
    transition: all var(--duration-fast) ease;
    box-shadow: none;
  }

  .action-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    pointer-events: none;
  }

  .action-btn:hover {
    background: var(--border-color);
    transform: none;
    box-shadow: none;
  }

  .action-btn.accept {
    background: var(--success-solid);
    color: var(--text-on-solid);
    border-color: var(--success-color);
  }

  .action-btn.accept:hover {
    opacity: 0.9;
    transform: none;
    box-shadow: none;
  }

  .assign-select {
    max-width: 170px;
    min-width: 0;
    flex-shrink: 1;
    padding: 0.3rem var(--space-2);
    font-size: var(--font-size-sm);
    cursor: pointer;
  }

  .assign-select:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .action-btn.skip {
    background: var(--card-background);
    color: var(--text-secondary);
    border-color: var(--border-color);
  }

  .action-btn.skip:hover {
    background: var(--button-hover);
    border-color: var(--text-secondary);
    color: var(--text-color);
    transform: none;
    box-shadow: none;
  }

  @media (max-width: 768px) {
    .inbox-row {
      padding: var(--space-2) var(--space-3);
      gap: var(--space-2);
    }

    .item-left {
      gap: var(--space-2);
    }

    .speaker-name {
      font-size: var(--font-size-sm);
    }

    .suggestion {
      font-size: var(--font-size-xs);
      gap: var(--space-1);
    }

    .suggestion-name {
      max-width: 140px;
    }

    .file-title {
      max-width: 120px;
    }

    .meta {
      font-size: var(--font-size-2xs);
      gap: var(--space-1);
    }

    .item-actions {
      gap: var(--space-1);
    }

    .action-btn {
      padding: var(--space-1) var(--space-2);
      font-size: var(--font-size-xs);
    }
  }

  @media (max-width: 480px) {
    .inbox-row {
      flex-wrap: wrap;
    }

    .item-left {
      flex-basis: 100%;
      min-width: 0;
    }

    .item-actions {
      margin-left: auto;
    }

    .suggestion-name {
      max-width: 120px;
    }

    .file-title {
      max-width: 100px;
    }
  }
</style>
