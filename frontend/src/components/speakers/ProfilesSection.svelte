<script lang="ts">
  import { SvelteSet } from 'svelte/reactivity';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import ProfilesTab from '$components/speakers/ProfilesTab.svelte';
  import {
    updateProfile,
    deleteProfile,
    uploadProfileAvatar,
    deleteProfileAvatar,
    confirmProfileGender
  } from '$lib/api/speakerClusters';
  import type { SpeakerProfile } from '$lib/types/speakerCluster';
  import { avatarFileError } from '$lib/speakers/pageHelpers';
  import { getErrorMessage } from '$lib/utils/apiError';

  /** The page owns loading; this owns the profile edit / delete / avatar / gender flows. */
  export let profiles: SpeakerProfile[] = [];
  export let loadingProfiles = false;
  export let reload: () => Promise<void>;
  /** Mounted-but-hidden keeps the delete modal's state out of the other tabs. */
  export let active = false;

  let avatarUploading: Set<string> = new SvelteSet();
  let genderConfirmedProfiles: Set<string> = new SvelteSet();
  let editingProfileUuid: string | null = null;
  let editProfileName = '';
  let deleteProfileUuid = '';
  let showDeleteProfileModal = false;
  let removeAvatarUuid = '';

  async function handleAvatarUpload(profileUuid: string, event: Event) {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    input.value = '';

    const invalid = avatarFileError(file);
    if (invalid) {
      toastStore.error($t(invalid));
      return;
    }

    if (avatarUploading.has(profileUuid)) return;
    avatarUploading.add(profileUuid);
    avatarUploading = avatarUploading;

    try {
      const result = await uploadProfileAvatar(profileUuid, file);
      // Update the profile in the local array
      profiles = profiles.map(p => p.uuid === profileUuid ? { ...p, avatar_url: result.avatar_url } : p);
      toastStore.success($t('speakers.avatar.uploaded'));
    } catch {
      toastStore.error($t('speakers.avatar.error'));
    } finally {
      avatarUploading.delete(profileUuid);
      avatarUploading = avatarUploading;
    }
  }

  async function handleRemoveAvatar() {
    const profileUuid = removeAvatarUuid;
    removeAvatarUuid = '';
    try {
      await deleteProfileAvatar(profileUuid);
      profiles = profiles.map(p => p.uuid === profileUuid ? { ...p, avatar_url: null } : p);
      toastStore.success($t('speakers.avatar.removed'));
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('speakers.avatar.removeError')));
    }
  }

  function startEditProfile(profile: SpeakerProfile) {
    editingProfileUuid = profile.uuid;
    editProfileName = profile.name || '';
  }

  function cancelEditProfile() {
    editingProfileUuid = null;
    editProfileName = '';
  }

  async function saveProfile(uuid: string) {
    if (!editProfileName.trim()) { cancelEditProfile(); return; }
    try {
      const updated = await updateProfile(uuid, { name: editProfileName.trim() });
      // Patch from the response, as the avatar and gender handlers already do — reloading
      // the list skeletoned the whole Profiles tab for a one-field edit.
      profiles = profiles.map(profile =>
        profile.uuid === uuid ? { ...profile, ...updated } : profile
      );
    } catch {
      toastStore.error($t('speakers.error.updateProfile'));
    }
    cancelEditProfile();
  }

  function confirmDeleteProfile(uuid: string) {
    deleteProfileUuid = uuid;
    showDeleteProfileModal = true;
  }

  async function handleDeleteProfile() {
    try {
      await deleteProfile(deleteProfileUuid);
      toastStore.success($t('speakers.profiles.deleted'));
      await reload();
    } catch {
      toastStore.error($t('speakers.error.deleteProfile'));
    }
    showDeleteProfileModal = false;
    deleteProfileUuid = '';
  }

  async function handleConfirmProfileGender(profile: SpeakerProfile, gender: string) {
    const oldGender = profile.predicted_gender;
    // Optimistic update
    profiles = profiles.map(p =>
      p.uuid === profile.uuid ? { ...p, predicted_gender: gender } : p
    );
    genderConfirmedProfiles.add(profile.uuid);
    genderConfirmedProfiles = genderConfirmedProfiles;
    try {
      await confirmProfileGender(profile.uuid, gender);
    } catch {
      // Revert on error
      profiles = profiles.map(p =>
        p.uuid === profile.uuid ? { ...p, predicted_gender: oldGender } : p
      );
      genderConfirmedProfiles.delete(profile.uuid);
      genderConfirmedProfiles = genderConfirmedProfiles;
      toastStore.error($t('speakers.error.confirmGender'));
    }
  }
</script>

<!-- Profiles Tab -->
{#if active}
  <ProfilesTab
    {loadingProfiles}
    {profiles}
    {avatarUploading}
    {editingProfileUuid}
    bind:editProfileName
    {genderConfirmedProfiles}
    on:avatarUpload={(e) => handleAvatarUpload(e.detail.uuid, e.detail.event)}
    on:removeAvatar={(e) => (removeAvatarUuid = e.detail)}
    on:startEdit={(e) => startEditProfile(e.detail)}
    on:saveProfile={(e) => saveProfile(e.detail)}
    on:cancelEdit={cancelEditProfile}
    on:confirmDelete={(e) => confirmDeleteProfile(e.detail)}
    on:confirmGender={(e) => handleConfirmProfileGender(e.detail.profile, e.detail.gender)}
  />
{/if}

<!-- Delete Profile Confirmation Modal -->
<ConfirmationModal
  isOpen={showDeleteProfileModal}
  title={$t('speakers.profiles.deleteTitle')}
  message={$t('speakers.profiles.deleteMessage')}
  confirmText={$t('modal.delete')}
  confirmButtonClass="confirm-button delete-confirm"
  on:confirm={handleDeleteProfile}
  on:cancel={() => { showDeleteProfileModal = false; }}
  on:close={() => { showDeleteProfileModal = false; }}
/>

<ConfirmationModal
  isOpen={removeAvatarUuid !== ''}
  title={$t('speakers.avatar.removeTitle')}
  message={$t('speakers.avatar.removeMessage')}
  confirmText={$t('speakers.avatar.removeConfirm')}
  confirmButtonClass="confirm-button delete-confirm"
  on:confirm={handleRemoveAvatar}
  on:cancel={() => { removeAvatarUuid = ''; }}
  on:close={() => { removeAvatarUuid = ''; }}
/>
