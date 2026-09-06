<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { SpeakerInboxItem as InboxItemType } from '$lib/types/speakerCluster';
  import { audioPlaybackStore } from '$stores/audioPlaybackStore';
  import { t } from '$stores/locale';

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
    if (confidence >= 0.75) return 'var(--success-color, #10b981)';
    if (confidence >= 0.5) return 'var(--warning-color, #f59e0b)';
    return 'var(--error-color, #ef4444)';
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
                {(item.confidence * 100).toFixed(0)}%
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
            <span class="attribute">{item.predicted_gender}</span>
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
          {#each profiles as profile}
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
    border-bottom: 1px solid var(--border-color, #e5e7eb);
    transition: background 0.1s ease;
  }

  .inbox-item:hover {
    background: var(--hover-color, #f9fafb);
  }

  .inbox-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px;
  }

  .item-left {
    display: flex;
    align-items: center;
    gap: 10px;
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
    border-radius: 6px;
    background: none;
    color: var(--primary-color, #4f46e5);
    cursor: pointer;
    transition: all 0.15s;
    box-shadow: none;
  }

  .preview-toggle:hover {
    background: color-mix(in srgb, var(--primary-color, #4f46e5) 10%, transparent);
    transform: scale(1.1);
    box-shadow: none;
  }

  .preview-toggle.playing {
    color: var(--primary-color, #4f46e5);
    background: color-mix(in srgb, var(--primary-color, #4f46e5) 12%, transparent);
  }

  .item-info {
    flex: 1;
    min-width: 0;
  }

  .speaker-name {
    font-weight: 500;
    color: var(--text-color, #111827);
    font-size: 14px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .suggestion {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 2px;
    font-size: 13px;
    flex-wrap: wrap;
    min-width: 0;
  }

  .suggestion-label {
    color: var(--text-secondary, #9ca3af);
  }

  .suggestion-name {
    color: var(--primary-color, #3b82f6);
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 200px;
  }

  .confidence {
    font-weight: 600;
    font-size: 12px;
  }

  .source-badge {
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 8px;
    background: var(--hover-color, #f3f4f6);
    color: var(--text-secondary, #9ca3af);
  }

  .source-badge.source-profile {
    background: color-mix(in srgb, var(--success-color, #10b981) 10%, transparent);
    color: var(--success-color, #10b981);
  }

  .source-badge.source-voice {
    background: color-mix(in srgb, var(--primary-color, #3b82f6) 10%, transparent);
    color: var(--primary-color, #3b82f6);
  }

  .source-badge.source-llm {
    background: color-mix(in srgb, var(--text-secondary, #9ca3af) 10%, transparent);
    color: var(--text-secondary, #9ca3af);
  }

  .meta {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 3px;
    font-size: 12px;
    color: var(--text-secondary, #9ca3af);
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
    padding: 1px 6px;
    border-radius: 8px;
    background: var(--hover-color, #f3f4f6);
  }

  .item-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
  }

  .action-btn {
    /* Never let a one-word action wrap. Adding the assign dropdown to this row
       squeezed Skip until it broke across two lines as "Ski"/"p". */
    white-space: nowrap;
    padding: 4px 12px;
    border-radius: 6px;
    border: 1px solid var(--border-color, #d1d5db);
    background: var(--hover-color, #f9fafb);
    color: var(--text-color, #374151);
    font-size: 13px;
    cursor: pointer;
    transition: all 0.15s ease;
    box-shadow: none;
  }

  .action-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    pointer-events: none;
  }

  .action-btn:hover {
    background: var(--border-color, #e5e7eb);
    transform: none;
    box-shadow: none;
  }

  .action-btn.accept {
    background: var(--success-color, #10b981);
    color: white;
    border-color: var(--success-color, #10b981);
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
    padding: 0.3rem 0.5rem;
    font-size: 0.8125rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--surface-color);
    color: var(--text-primary);
    cursor: pointer;
  }

  .assign-select:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .action-btn.skip {
    background: var(--card-background, #fff);
    color: var(--text-secondary, #6b7280);
    border-color: var(--border-color, #d1d5db);
  }

  .action-btn.skip:hover {
    background: var(--button-hover, #e5e7eb);
    border-color: var(--text-secondary, #9ca3af);
    color: var(--text-color, #374151);
    transform: none;
    box-shadow: none;
  }

  @media (max-width: 768px) {
    .inbox-row {
      padding: 8px 12px;
      gap: 8px;
    }

    .item-left {
      gap: 8px;
    }

    .speaker-name {
      font-size: 13px;
    }

    .suggestion {
      font-size: 12px;
      gap: 4px;
    }

    .suggestion-name {
      max-width: 140px;
    }

    .file-title {
      max-width: 120px;
    }

    .meta {
      font-size: 11px;
      gap: 4px;
    }

    .item-actions {
      gap: 4px;
    }

    .action-btn {
      padding: 4px 8px;
      font-size: 12px;
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
