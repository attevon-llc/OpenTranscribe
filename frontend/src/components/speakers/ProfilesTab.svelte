<script lang="ts">
  import { focusOnMount } from '$lib/actions/focusOnMount';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import CardGridSkeleton from '$components/ui/CardGridSkeleton.svelte';
  import type { SpeakerProfile } from '$lib/types/speakerCluster';

  export let loadingProfiles = false;
  export let profiles: SpeakerProfile[] = [];
  export let avatarUploading: Set<string> = new Set();
  export let editingProfileUuid: string | null = null;
  export let editProfileName = '';
  export let genderConfirmedProfiles: Set<string> = new Set();

  const dispatch = createEventDispatcher();

  function getInitials(name: string): string {
    return name.split(/\s+/).map(w => w[0]).filter(Boolean).slice(0, 2).join('').toUpperCase();
  }

  function pickAvatar(uuid: string) {
    if (avatarUploading.has(uuid)) return;
    document.getElementById('avatar-input-' + uuid)?.click();
  }
</script>

<div class="tab-content">
  {#if loadingProfiles}
    <CardGridSkeleton variant="profile" count={8} minCardWidth={280} />
  {:else if profiles.length === 0}
    <EmptyState title={$t('speakers.profiles.emptyTitle')} description={$t('speakers.profiles.emptyDesc')} padding="60px 20px">
      <svelte:fragment slot="icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" />
        </svg>
      </svelte:fragment>
    </EmptyState>
  {:else}
    <div class="profile-list">
      {#each profiles as profile (profile.uuid)}
        <div class="profile-card">
          <div class="profile-header">
            <div class="profile-avatar-wrapper" role="button" tabindex="0" on:click|stopPropagation={() => pickAvatar(profile.uuid)} on:keydown={(e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); pickAvatar(profile.uuid); } }} title={$t('speakers.tooltip.uploadAvatar')}>
              {#if avatarUploading.has(profile.uuid)}
                <div class="avatar-spinner"><Spinner label={$t('common.loading')} size="small" /></div>
              {:else if profile.avatar_url}
                <img
                  class="profile-avatar"
                  src={profile.avatar_url}
                  alt={profile.name || 'Speaker avatar'}
                  loading="lazy"
                  decoding="async"
                />
                <div class="avatar-overlay">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
                </div>
              {:else}
                <div class="avatar-initials">{getInitials(profile.name || '?')}</div>
                <div class="avatar-overlay">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>
                </div>
              {/if}
              <input id="avatar-input-{profile.uuid}" type="file" accept="image/jpeg,image/png,image/gif,image/webp" style="display:none" on:change={(e) => dispatch('avatarUpload', { uuid: profile.uuid, event: e })} />
            </div>
            {#if editingProfileUuid === profile.uuid}
              <input class="profile-edit-input" bind:value={editProfileName}
                on:keydown={(e) => { if (e.key === 'Enter') dispatch('saveProfile', profile.uuid); if (e.key === 'Escape') dispatch('cancelEdit'); }}
                on:blur={() => dispatch('saveProfile', profile.uuid)}
                use:focusOnMount />
            {:else}
              <div class="profile-name">{profile.name || $t('speakers.profiles.unnamed')}</div>
              {#if profile.is_shared}
                <span class="shared-badge" title={profile.owner_name ? $t('speakers.profiles.sharedBy', { name: profile.owner_name }) : $t('speakers.profiles.shared')}>{$t('speakers.profiles.shared')}</span>
              {/if}
            {/if}
            <div class="profile-actions">
              {#if !profile.is_shared}
                {#if profile.avatar_url}
                  <button class="icon-btn" on:click={() => dispatch('removeAvatar', profile.uuid)} title={$t('speakers.avatar.remove')} aria-label={$t('speakers.avatar.remove')}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <circle cx="12" cy="8" r="4" />
                      <path d="M4 21v-1a6 6 0 0 1 6-6h4" />
                      <line x1="16" y1="18" x2="22" y2="18" />
                    </svg>
                  </button>
                {/if}
                <button class="icon-btn" on:click={() => dispatch('startEdit', profile)} title={$t('speakers.profiles.edit')}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                  </svg>
                </button>
                <button class="icon-btn danger" on:click={() => dispatch('confirmDelete', profile.uuid)} title={$t('speakers.profiles.deleteBtn')}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="3 6 5 6 21 6" />
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  </svg>
                </button>
              {/if}
            </div>
          </div>
          {#if profile.description}
            <div class="profile-desc">{profile.description}</div>
          {/if}
          <div class="profile-meta">
            <span class="meta-stat" title="{$t('speakers.profiles.instances')}">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/></svg>
              {profile.instance_count || 0}
            </span>
            <span class="meta-stat" title="{$t('speakers.profiles.files')}">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
              {profile.media_count || 0}
            </span>
            <span class="gender-confirm-group">
              <button
                class="gender-toggle-btn"
                class:active={profile.predicted_gender === 'male'}
                on:click|stopPropagation={() => dispatch('confirmGender', { profile, gender: 'male' })}
                title={$t('speakers.profiles.confirmMale')}
                aria-label={$t('speakers.profiles.confirmMale')}
              >
                <svg class="gender-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="14" r="7"/><line x1="15" y1="9" x2="21" y2="3"/><polyline points="15 3 21 3 21 9"/></svg>
                <span class="gender-toggle-label">{$t('speakers.member.male')}</span>
                {#if profile.predicted_gender === 'male' && genderConfirmedProfiles.has(profile.uuid)}<span class="gender-confirmed-tick" aria-hidden="true"><MenuIcon name="check" size={10} /></span>{/if}
              </button>
              <button
                class="gender-toggle-btn"
                class:active={profile.predicted_gender === 'female'}
                on:click|stopPropagation={() => dispatch('confirmGender', { profile, gender: 'female' })}
                title={$t('speakers.profiles.confirmFemale')}
                aria-label={$t('speakers.profiles.confirmFemale')}
              >
                <svg class="gender-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="9" r="7"/><line x1="12" y1="16" x2="12" y2="23"/><line x1="9" y1="20" x2="15" y2="20"/></svg>
                <span class="gender-toggle-label">{$t('speakers.member.female')}</span>
                {#if profile.predicted_gender === 'female' && genderConfirmedProfiles.has(profile.uuid)}<span class="gender-confirmed-tick" aria-hidden="true"><MenuIcon name="check" size={10} /></span>{/if}
              </button>
            </span>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .gender-confirm-group {
    display: inline-flex;
    gap: var(--space-1);
    align-items: center;
  }

  .gender-toggle-btn {
    font-size: var(--font-size-md);
    padding: var(--space-1) var(--space-1-5);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-color);
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
    transition: all var(--duration-fast) ease;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    gap: var(--space-0-5);
  }

  .gender-svg {
    width: 14px;
    height: 14px;
    flex-shrink: 0;
  }

  .gender-toggle-label {
    font-size: var(--font-size-2xs);
  }

  .gender-toggle-btn:hover {
    border-color: var(--primary-color);
    color: var(--primary-on-surface);
  }

  .gender-toggle-btn.active {
    border-color: var(--primary-color);
    background: color-mix(in srgb, var(--primary-color) 12%, transparent);
    color: var(--primary-on-surface);
  }

  .profile-list {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: var(--space-3);
    max-width: 100%;
  }

  .profile-card {
    padding: var(--space-4);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background: var(--card-background);
    transition: box-shadow var(--duration-fast) ease;
    overflow: hidden;
    min-width: 0;
  }

  .profile-card:hover {
    box-shadow: var(--shadow-sm);
  }

  .profile-avatar-wrapper {
    width: 48px;
    height: 48px;
    border-radius: var(--radius-round);
    flex-shrink: 0;
    cursor: pointer;
    position: relative;
    overflow: hidden;
  }

  .avatar-overlay {
    position: absolute;
    inset: 0;
    border-radius: var(--radius-round);
    background: var(--media-scrim);
    display: flex;
    align-items: center;
    justify-content: center;
    opacity: 0;
    transition: opacity var(--duration-fast) ease;
    color: var(--text-on-solid);
  }

  .profile-avatar-wrapper:hover .avatar-overlay {
    opacity: 1;
  }

  .profile-avatar {
    width: 48px;
    height: 48px;
    border-radius: var(--radius-round);
    object-fit: cover;
  }

  .avatar-initials {
    width: 48px;
    height: 48px;
    border-radius: var(--radius-round);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-semibold);
    color: var(--primary-on-surface);
    background: color-mix(in srgb, var(--primary-color) 20%, transparent);
  }

  .avatar-spinner {
    width: 48px;
    height: 48px;
    border-radius: var(--radius-round);
    display: flex;
    align-items: center;
    justify-content: center;
    background: color-mix(in srgb, var(--primary-color) 20%, transparent);
  }

  .profile-header {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    min-width: 0;
  }

  .profile-name {
    font-weight: var(--font-weight-semibold);
    font-size: var(--font-size-body);
    color: var(--text-color);
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .profile-desc {
    margin-top: var(--space-1);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    overflow-wrap: anywhere;
  }

  .profile-meta {
    margin-top: var(--space-2);
    display: flex;
    align-items: center;
    gap: var(--space-2-5);
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    flex-wrap: wrap;
  }

  .meta-stat {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    font-weight: var(--font-weight-medium);
    cursor: default;
  }

  .meta-stat svg {
    opacity: 0.6;
    flex-shrink: 0;
  }

  .profile-actions {
    display: flex;
    gap: var(--space-1);
    flex-shrink: 0;
  }

  .icon-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    min-width: 28px;
    padding: 0;
    border-radius: var(--radius-md);
    border: 1px solid var(--border-color);
    background: var(--card-background);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all var(--duration-fast) ease;
    box-shadow: none;
    font-size: 0;
  }

  .icon-btn:hover {
    background: var(--hover-color);
    color: var(--text-color);
    transform: none;
    box-shadow: none;
  }

  .icon-btn.danger:hover {
    color: var(--error-text);
    background: color-mix(in srgb, var(--error-color) 10%, transparent);
    border-color: color-mix(in srgb, var(--error-color) 30%, transparent);
    transform: none;
    box-shadow: none;
  }

  .profile-edit-input {
    flex: 1;
    min-width: 0;
    padding: var(--space-1) var(--space-2);
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
    box-sizing: border-box;
  }

  .shared-badge {
    display: inline-block;
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    padding: 1px var(--space-1-5);
    border-radius: var(--radius-lg);
    background: var(--accent-light);
    color: var(--primary-on-surface);
    margin-inline-start: var(--space-1-5);
    vertical-align: middle;
    white-space: nowrap;
  }

  @media (max-width: 768px) {
    .profile-list {
      grid-template-columns: 1fr;
      gap: var(--space-2-5);
    }

    .profile-card {
      padding: var(--space-3);
    }

    .profile-header {
      gap: var(--space-2);
    }

    .profile-avatar-wrapper,
    .profile-avatar,
    .avatar-initials,
    .avatar-spinner {
      width: 40px;
      height: 40px;
    }

    .avatar-initials {
      font-size: var(--font-size-md);
    }

    .profile-name {
      font-size: var(--font-size-md);
    }

    .profile-meta {
      gap: var(--space-2);
    }
  }
</style>
