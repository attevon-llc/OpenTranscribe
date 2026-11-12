<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { GROUP_MAPPING_ROLE_KEYS } from '$lib/i18n/keyMaps';
  import { createEventDispatcher } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import {
    GroupMappingsApi,
    GRANTABLE_ROLES,
    type GroupMapping,
    type GroupMappingSource,
    type GrantableRole
  } from '$lib/api/groupMappings';
  import type { Group } from '$lib/types/groups';

  export let isOpen = false;
  export let source: GroupMappingSource = 'ldap';
  /** Existing mapping to edit; `null` creates a new one. */
  export let mapping: GroupMapping | null = null;
  /** Picker options, loaded once by the parent from `/api/groups`. */
  export let groups: Group[] = [];

  const dispatch = createEventDispatcher<{ saved: GroupMapping; close: void }>();

  let claimValue = '';
  let groupUuid = '';
  /**
   * `''` means "grant no role". `super_admin` is deliberately not an option and
   * must never become one: the wire schema, the service and a DB CHECK all refuse
   * it, so offering it would only produce a 400 — and it would advertise a
   * privilege escalation path that does not exist.
   */
  let grantsRole: GrantableRole | '' = '';
  let description = '';
  let saving = false;

  // Re-seed whenever the modal is (re)opened, so editing one mapping and then
  // creating another does not inherit the previous values.
  $: if (isOpen) seed(mapping);

  function seed(existing: GroupMapping | null) {
    claimValue = existing?.claim_value ?? '';
    groupUuid = existing?.group_uuid ?? '';
    grantsRole = existing?.grants_role ?? '';
    description = existing?.description ?? '';
  }

  // Mirrors the backend's "a mapping must grant something" rule (enforced in the
  // Pydantic model, the service and the database) so the refusal is visible
  // before a round trip rather than as a 400 afterwards.
  $: grantsNothing = groupUuid === '' && grantsRole === '';
  $: canSubmit = claimValue.trim() !== '' && !grantsNothing && !saving;

  function handleClose() {
    isOpen = false;
    dispatch('close');
  }

  async function handleSubmit() {
    if (!canSubmit) return;
    saving = true;
    try {
      const saved = mapping
        ? await GroupMappingsApi.update(mapping.uuid, {
            claim_value: claimValue.trim(),
            group_uuid: groupUuid || null,
            grants_role: grantsRole || null,
            description: description.trim() || null
          })
        : await GroupMappingsApi.create({
            source,
            claim_value: claimValue.trim(),
            group_uuid: groupUuid || null,
            grants_role: grantsRole || null,
            description: description.trim() || null
          });
      toastStore.success(
        mapping
          ? $t('settings.groupMappings.toast.updated')
          : $t('settings.groupMappings.toast.created')
      );
      dispatch('saved', saved);
      handleClose();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.groupMappings.toast.saveFailed')));
    } finally {
      saving = false;
    }
  }
</script>

<BaseModal
  {isOpen}
  title={mapping
    ? $t('settings.groupMappings.editTitle')
    : $t('settings.groupMappings.addTitle')}
  size="md"
  zIndex={1300}
  onClose={handleClose}
>
  <form id="group-mapping-form" on:submit|preventDefault={handleSubmit}>
    <div class="modal-body">
      <FormField id="mapping-claim" label={$t('settings.groupMappings.claimValue')}>
        <input dir="ltr"
          type="text"
          id="mapping-claim"
          class="form-control"
          bind:value={claimValue}
          maxlength="1024"
          placeholder={source === 'ldap'
            ? $t('settings.groupMappings.claimValuePlaceholderLdap')
            : $t('settings.groupMappings.claimValuePlaceholderOidc')}
          required
        />
        <span class="form-hint">
          {source === 'ldap'
            ? $t('settings.groupMappings.claimValueHelpLdap')
            : $t('settings.groupMappings.claimValueHelpOidc')}
        </span>
      </FormField>

      <FormField id="mapping-group" label={$t('settings.groupMappings.targetGroup')} hint={$t('settings.groupMappings.targetGroupHelp')} let:describedby>
        <select id="mapping-group" aria-describedby={describedby} class="form-control" bind:value={groupUuid}>
          <option value="">{$t('settings.groupMappings.noGroup')}</option>
          {#each groups as group (group.uuid)}
            <option value={group.uuid}>{group.name}</option>
          {/each}
        </select>
      </FormField>

      <FormField id="mapping-role" label={$t('settings.groupMappings.grantsRole')} hint={$t('settings.groupMappings.grantsRoleHelp')} let:describedby>
        <select id="mapping-role" aria-describedby={describedby} class="form-control" bind:value={grantsRole}>
          <option value="">{$t('settings.groupMappings.noRole')}</option>
          {#each GRANTABLE_ROLES as role (role)}
            <option value={role}>{$t(GROUP_MAPPING_ROLE_KEYS[role])}</option>
          {/each}
        </select>
      </FormField>

      <FormField id="mapping-description" label={$t('settings.groupMappings.descriptionLabel')}>
        <textarea
          id="mapping-description"
          class="form-control form-textarea"
          bind:value={description}
          rows="2"
          maxlength="2000"
          placeholder={$t('settings.groupMappings.descriptionPlaceholder')}
        ></textarea>
      </FormField>

      {#if grantsNothing}
        <p class="warning-note warning-banner" role="alert">
          {$t('settings.groupMappings.grantsNothingWarning')}
        </p>
      {/if}
    </div>
  </form>

  <svelte:fragment slot="footer">
    <button type="button" class="btn btn-secondary" on:click={handleClose}>
      {$t('modal.cancel')}
    </button>
    <button type="submit" form="group-mapping-form" class="btn btn-primary" disabled={!canSubmit}>
      {saving ? $t('common.saving') : $t('common.save')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .modal-body {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .form-control {
    padding: var(--space-2) var(--space-2-5);
    font-size: var(--font-size-sm);
  }

  .form-textarea {
    resize: vertical;
    min-height: 56px;
  }

  .form-hint {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.45;
  }

  .warning-banner {
    font-size: var(--font-size-sm);
    line-height: 1.45;
  }

  @media (max-width: 768px) {
    .form-control {
      min-height: 44px;
      font-size: var(--font-size-base);
    }
  }
</style>
