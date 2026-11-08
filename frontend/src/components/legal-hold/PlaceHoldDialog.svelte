<!--
  PlaceHoldDialog.svelte - the ONE "Place legal hold" dialog (legal-hold plan, Lane D).

  Used by the gallery selection, the file page and the Settings holds panel. With `fileUuids`
  the target is fixed (the gallery selection); without it the person searches for files by
  title or file name and selects them (`FileSearchPicker`), or chooses a collection. `placer` picks the API: an organization
  administrator's `/org-admin/holds` or the operator's `/admin/holds`
  (`holdAuthorityForCaller`). The server decides everything that matters; this keeps "Place"
  disabled until the input is one the server accepts and shows the answer.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import HoldExplainer from '$components/legal-hold/HoldExplainer.svelte';
  import FileSearchPicker from '$components/ui/FileSearchPicker.svelte';
  import { CollectionsApi } from '$lib/api/collections';
  import type { OrgHold } from '$lib/api/orgAdmin';
  import {
    HOLD_MATTER_MAX,
    HOLD_MAX_FILES,
    HOLD_REASON_MAX,
    HOLD_REASON_MIN,
    holdReasonValid,
    type HoldPlacer,
  } from '$lib/legalHold/authority';
  import { holdsApiFor } from '$lib/legalHold/holdsApi';
  import type { Collection } from '$lib/types/collection';
  import type { MediaFile } from '$lib/types/media';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  export let isOpen = false;
  export let placer: HoldPlacer = 'platform';
  /** A fixed target (the gallery selection). `null` lets the person choose. */
  export let fileUuids: string[] | null = null;
  /** The file's name when exactly one fixed file is held, shown instead of a count. */
  export let fileLabel = '';

  const dispatch = createEventDispatcher<{ close: void; placed: { hold: OrgHold; fileUuids: string[] } }>();

  let scope: 'file' | 'collection' = 'file';
  let pickedFiles: MediaFile[] = [];
  let collectionUuid = '';
  let collections: Collection[] = [];
  let reason = '';
  let matterRef = '';
  let saving = false;
  let lastOpen = false;

  // Through a function on purpose: two `$:` lines reading and writing `lastOpen` would be
  // reordered by Svelte so that the reset never fires.
  $: onOpenChange(isOpen);
  function onOpenChange(open: boolean) {
    if (open && !lastOpen) reset();
    lastOpen = open;
  }

  $: chooser = fileUuids === null;
  $: pickedUuids = pickedFiles.map((f) => f.uuid);
  $: pickedValid = pickedUuids.length > 0 && pickedUuids.length <= HOLD_MAX_FILES;
  $: tooMany = !chooser && (fileUuids?.length ?? 0) > HOLD_MAX_FILES;
  $: targetValid = chooser
    ? scope === 'file'
      ? pickedValid
      : collectionUuid !== ''
    : (fileUuids?.length ?? 0) > 0 && !tooMany;
  $: reasonLength = reason.trim().length;
  $: reasonValid = holdReasonValid(reason);
  $: canPlace = targetValid && reasonValid && !saving;
  $: reasonHelpKey = placer === 'organization' ? 'holds.reasonHelp' : 'holds.platform.reasonHelp';

  async function reset() {
    scope = 'file';
    pickedFiles = [];
    collectionUuid = '';
    reason = '';
    matterRef = '';
    saving = false;
    if (fileUuids !== null) return;
    try {
      collections = await CollectionsApi.list();
    } catch (error) {
      console.error('Failed to load collections for a hold:', error);
      collections = [];
    }
  }

  function close() {
    dispatch('close');
  }

  async function place() {
    if (!canPlace) return;
    saving = true;
    const target = chooser
      ? scope === 'file'
        ? { scope: 'file' as const, file_uuids: pickedUuids }
        : { scope: 'collection' as const, collection_uuid: collectionUuid }
      : { scope: 'file' as const, file_uuids: fileUuids ?? [] };
    try {
      const hold = await holdsApiFor(placer).placeHold({
        ...target,
        reason: reason.trim(),
        matter_ref: matterRef.trim() || undefined,
      });
      toastStore.success($t('holds.placed', { count: hold.file_count }));
      dispatch('placed', { hold, fileUuids: target.file_uuids ?? [] });
      close();
    } catch (error) {
      console.error('Failed to place a hold:', error);
      toastStore.error(getErrorMessage(error, $t('holds.placeError')));
    } finally {
      saving = false;
    }
  }
</script>

<BaseModal {isOpen} title={$t('holds.placeTitle')} size="md" onClose={close}>
  <form class="place-hold" on:submit|preventDefault={place} data-testid="place-hold-dialog">
    {#if chooser}
      <fieldset class="scope">
        <legend>{$t('holds.scopeLabel')}</legend>
        <label>
          <input type="radio" bind:group={scope} value="file" />
          {$t('holds.scopeFiles')}
        </label>
        <label>
          <input type="radio" bind:group={scope} value="collection" />
          {$t('holds.scopeCollection')}
        </label>
      </fieldset>

      {#if scope === 'collection'}
        <label class="field">
          <span>{$t('holds.collectionLabel')}</span>
          <select bind:value={collectionUuid}>
            <option value="">{$t('holds.collectionChoose')}</option>
            {#each collections as collection (collection.uuid)}
              <option value={collection.uuid}>{collection.name}</option>
            {/each}
          </select>
        </label>
      {:else}
        <FileSearchPicker
          bind:selected={pickedFiles}
          max={HOLD_MAX_FILES}
          label={$t('holds.pickFilesLabel')}
          help={$t('holds.pickFilesHelp', { max: HOLD_MAX_FILES })}
          disabled={saving}
        />
      {/if}
    {:else}
      <p class="target" data-testid="place-hold-target">
        {#if fileUuids?.length === 1 && fileLabel}
          <span dir="auto">{fileLabel}</span>
        {:else}
          {$t('holds.dialog.targetFiles', { count: fileUuids?.length ?? 0 })}
        {/if}
      </p>
      {#if tooMany}
        <p class="error" role="alert">{$t('holds.dialog.tooMany', { max: HOLD_MAX_FILES })}</p>
      {/if}
    {/if}

    <label class="field">
      <span>{$t('holds.reasonLabel')}</span>
      <textarea
        rows="3"
        maxlength={HOLD_REASON_MAX}
        bind:value={reason}
        aria-invalid={reason.trim() !== '' && !reasonValid}
        aria-describedby="place-hold-reason-help"
        data-testid="place-hold-reason"
      ></textarea>
      <span class="reason-meta" id="place-hold-reason-help">
        <small>{$t(reasonHelpKey, { min: HOLD_REASON_MIN, max: HOLD_REASON_MAX })}</small>
        <small
          class="counter"
          class:error={reason.trim() !== '' && !reasonValid}
          dir="ltr"
          aria-live="polite"
        >
          {$t('holds.dialog.reasonCount', { count: reasonLength, max: HOLD_REASON_MAX })}
        </small>
      </span>
    </label>

    <label class="field">
      <span>{$t('holds.matterLabel')}</span>
      <input type="text" maxlength={HOLD_MATTER_MAX} bind:value={matterRef} />
    </label>

    <HoldExplainer />
  </form>

  <svelte:fragment slot="footer">
    <button class="btn btn-secondary" type="button" on:click={close}>{$t('common.cancel')}</button>
    <button
      class="btn btn-primary"
      type="button"
      disabled={!canPlace}
      on:click={place}
      data-testid="place-hold-submit"
    >
      {$t('holds.confirmPlace')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .place-hold {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    padding-block: var(--space-1);
    font-size: var(--font-size-sm);
  }

  .scope {
    border: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-4);
  }

  .scope legend {
    margin-block-end: var(--space-1);
    font-weight: var(--font-weight-semibold);
  }

  .scope label {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
  }

  .scope input {
    width: auto;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .field > span {
    font-weight: var(--font-weight-semibold);
  }

  .field small,
  .reason-meta small {
    color: var(--text-secondary);
  }

  .reason-meta {
    display: flex;
    justify-content: space-between;
    gap: var(--space-3);
  }

  .counter {
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
  }

  .target {
    margin: 0;
    font-weight: var(--font-weight-medium);
  }

  small.error,
  .error {
    color: var(--color-error-text);
  }

  .error {
    margin: 0;
  }
</style>
