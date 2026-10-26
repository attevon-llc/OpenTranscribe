<!--
  OrgLegalHoldsPanel.svelte — an organization administrator's legal holds (issue #1276).

  List, place and release. The server decides everything that matters (which files are the
  organization's, whether a reason is long enough, what is still held after a release); this
  renders it, sends the request and shows the answer. The reason is shown here and nowhere else:
  the badge on a file or collection says only that it is held and by whom.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Badge from '$components/ui/Badge.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { CollectionsApi } from '$lib/api/collections';
  import { OrgAdminApi, type OrgHeldFile, type OrgHold } from '$lib/api/orgAdmin';
  import type { Collection } from '$lib/types/collection';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { t, locale } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  /** The bounds the server enforces on a reason (`ck_content_hold_reason_length`). */
  const REASON_MIN = 10;
  const REASON_MAX = 2000;
  const MAX_FILES = 500;
  const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  let holds: OrgHold[] = [];
  let total = 0;
  let loading = true;
  let showReleased = false;

  let placing = false;
  let placeScope: 'file' | 'collection' = 'collection';
  let fileIdsText = '';
  let collectionUuid = '';
  let collections: Collection[] = [];
  let reason = '';
  let matterRef = '';
  let saving = false;

  /** Files of the holds the administrator expanded, by hold uuid. */
  let expanded: Record<string, OrgHeldFile[]> = {};

  let releasing: OrgHold | null = null;
  let releaseReason = '';

  $: fileIds = fileIdsText
    .split(/\s+/)
    .map((s) => s.trim())
    .filter(Boolean);
  $: filesValid =
    fileIds.length > 0 && fileIds.length <= MAX_FILES && fileIds.every((id) => UUID_PATTERN.test(id));
  $: targetValid = placeScope === 'file' ? filesValid : collectionUuid !== '';
  $: reasonValid = reason.trim().length >= REASON_MIN && reason.trim().length <= REASON_MAX;
  $: releaseReasonValid =
    releaseReason.trim().length >= REASON_MIN && releaseReason.trim().length <= REASON_MAX;
  $: canPlace = targetValid && reasonValid && !saving;

  async function load() {
    loading = true;
    try {
      const result = await OrgAdminApi.listHolds({ active_only: !showReleased, limit: 200 });
      holds = result.holds;
      total = result.total;
    } catch (error) {
      console.error('Failed to load legal holds:', error);
      toastStore.error($t('holds.loadError'));
    } finally {
      loading = false;
    }
  }

  async function openPlace() {
    placing = true;
    placeScope = 'collection';
    fileIdsText = '';
    collectionUuid = '';
    reason = '';
    matterRef = '';
    try {
      collections = await CollectionsApi.list();
    } catch (error) {
      console.error('Failed to load collections for a hold:', error);
      collections = [];
    }
  }

  async function place() {
    if (!canPlace) return;
    saving = true;
    try {
      const hold = await OrgAdminApi.placeHold({
        scope: placeScope,
        ...(placeScope === 'file'
          ? { file_uuids: fileIds }
          : { collection_uuid: collectionUuid }),
        reason: reason.trim(),
        matter_ref: matterRef.trim() || undefined,
      });
      toastStore.success($t('holds.placed', { count: hold.file_count }));
      placing = false;
      await load();
    } catch (error) {
      console.error('Failed to place a hold:', error);
      toastStore.error(getErrorMessage(error, $t('holds.placeError')));
    } finally {
      saving = false;
    }
  }

  async function toggleFiles(hold: OrgHold) {
    if (expanded[hold.uuid]) {
      const { [hold.uuid]: _closed, ...rest } = expanded;
      expanded = rest;
      return;
    }
    try {
      const detail = await OrgAdminApi.getHold(hold.uuid);
      expanded = { ...expanded, [hold.uuid]: detail.files };
    } catch (error) {
      console.error('Failed to load the files of a hold:', error);
      toastStore.error(getErrorMessage(error, $t('holds.loadError')));
    }
  }

  function openRelease(hold: OrgHold) {
    releasing = hold;
    releaseReason = '';
  }

  async function release() {
    if (!releasing || !releaseReasonValid) return;
    saving = true;
    try {
      await OrgAdminApi.releaseHold(releasing.uuid, releaseReason.trim());
      toastStore.success($t('holds.released'));
      releasing = null;
      await load();
    } catch (error) {
      console.error('Failed to release a hold:', error);
      toastStore.error(getErrorMessage(error, $t('holds.releaseError')));
    } finally {
      saving = false;
    }
  }

  const formatDate = (iso: string) =>
    new Date(iso).toLocaleDateString($locale || 'en', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });

  onMount(load);
</script>

<div class="holds">
  <p class="intro">{$t('holds.description')}</p>

  <div class="toolbar">
    <button class="btn-primary" type="button" on:click={openPlace}>{$t('holds.place')}</button>
    <label class="show-released">
      <input type="checkbox" bind:checked={showReleased} on:change={load} />
      {$t('holds.showReleased')}
    </label>
  </div>

  {#if loading}
    <SettingsSkeleton variant="form" count={3} label={$t('common.loading')} />
  {:else if holds.length === 0}
    <div class="empty">
      <MenuIcon name="shield" size={20} />
      <p>{$t('holds.empty')}</p>
    </div>
  {:else}
    <div class="table-wrap">
      <table class="holds-table">
        <thead>
          <tr>
            <th>{$t('holds.col.target')}</th>
            <th>{$t('holds.col.matter')}</th>
            <th>{$t('holds.col.placed')}</th>
            <th>{$t('holds.col.files')}</th>
            <th>{$t('holds.col.status')}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each holds as hold (hold.uuid)}
            <tr class:released={!hold.active}>
              <td>
                {#if hold.scope === 'collection'}
                  {$t('holds.collectionTarget', {
                    name: hold.collection_name ?? $t('holds.collectionGone'),
                  })}
                {:else}
                  {$t('holds.filesTarget')}
                {/if}
                <div class="reason">{hold.reason}</div>
              </td>
              <td dir="auto">{hold.matter_ref ?? '-'}</td>
              <td>
                {formatDate(hold.placed_at)}
                {#if hold.placed_by_display}
                  <div class="sub">{$t('holds.placedBy', { name: hold.placed_by_display })}</div>
                {/if}
              </td>
              <td>
                {hold.file_count}
                {#if hold.file_count > 0}
                  <button class="link" type="button" on:click={() => toggleFiles(hold)}>
                    {expanded[hold.uuid] ? $t('holds.hideFiles') : $t('holds.showFiles')}
                  </button>
                {/if}
                {#if expanded[hold.uuid]}
                  <ul class="files">
                    {#each expanded[hold.uuid] as file (file.uuid)}
                      <li dir="auto">{file.title || file.filename || file.uuid}</li>
                    {/each}
                  </ul>
                {/if}
              </td>
              <td>
                <Badge variant={hold.active ? 'warning' : 'default'}>
                  {hold.active ? $t('holds.status.active') : $t('holds.status.released')}
                </Badge>
                {#if hold.released_at}
                  <div class="sub">{$t('holds.releasedAt', { date: formatDate(hold.released_at) })}</div>
                {/if}
              </td>
              <td>
                {#if hold.active}
                  <button class="btn-secondary" type="button" on:click={() => openRelease(hold)}>
                    {$t('holds.release')}
                  </button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if total > holds.length}
      <p class="sub">{holds.length} / {total}</p>
    {/if}
  {/if}
</div>

<BaseModal isOpen={placing} title={$t('holds.placeTitle')} size="md" onClose={() => (placing = false)}>
  <div class="form">
    <fieldset class="scope">
      <legend>{$t('holds.scopeLabel')}</legend>
      <label>
        <input type="radio" bind:group={placeScope} value="collection" />
        {$t('holds.scopeCollection')}
      </label>
      <label>
        <input type="radio" bind:group={placeScope} value="file" />
        {$t('holds.scopeFiles')}
      </label>
    </fieldset>

    {#if placeScope === 'collection'}
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
      <label class="field">
        <span>{$t('holds.filesLabel')}</span>
        <textarea
          rows="4"
          dir="ltr"
          spellcheck="false"
          bind:value={fileIdsText}
          aria-invalid={fileIdsText.trim() !== '' && !filesValid}
        ></textarea>
        <small class:error={fileIdsText.trim() !== '' && !filesValid}>
          {fileIdsText.trim() !== '' && !filesValid
            ? $t('holds.invalidFileIds', { max: MAX_FILES })
            : $t('holds.filesHelp')}
        </small>
      </label>
    {/if}

    <label class="field">
      <span>{$t('holds.reasonLabel')}</span>
      <textarea
        rows="3"
        maxlength={REASON_MAX}
        bind:value={reason}
        aria-invalid={reason.trim() !== '' && !reasonValid}
      ></textarea>
      <small>{$t('holds.reasonHelp', { min: REASON_MIN, max: REASON_MAX })}</small>
    </label>

    <label class="field">
      <span>{$t('holds.matterLabel')}</span>
      <input type="text" maxlength="255" bind:value={matterRef} />
    </label>
  </div>

  <svelte:fragment slot="footer">
    <button class="btn-secondary" type="button" on:click={() => (placing = false)}>
      {$t('common.cancel')}
    </button>
    <button class="btn-primary" type="button" disabled={!canPlace} on:click={place}>
      {$t('holds.confirmPlace')}
    </button>
  </svelte:fragment>
</BaseModal>

<BaseModal
  isOpen={releasing !== null}
  title={$t('holds.releaseTitle')}
  size="sm"
  onClose={() => (releasing = null)}
>
  <p class="release-body">{$t('holds.releaseBody')}</p>
  <label class="field">
    <span>{$t('holds.releaseReasonLabel')}</span>
    <textarea
      rows="3"
      maxlength={REASON_MAX}
      bind:value={releaseReason}
      aria-invalid={releaseReason.trim() !== '' && !releaseReasonValid}
    ></textarea>
    <small>{$t('holds.reasonHelp', { min: REASON_MIN, max: REASON_MAX })}</small>
  </label>
  <svelte:fragment slot="footer">
    <button class="btn-secondary" type="button" on:click={() => (releasing = null)}>
      {$t('common.cancel')}
    </button>
    <button
      class="btn-primary"
      type="button"
      disabled={!releaseReasonValid || saving}
      on:click={release}
    >
      {$t('holds.confirmRelease')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .holds {
    padding: 0.5rem;
    font-size: 0.8125rem;
  }

  .intro {
    margin: 0 0 0.75rem;
    color: var(--color-text-secondary);
  }

  .toolbar {
    display: flex;
    align-items: center;
    gap: 1rem;
    margin-bottom: 0.75rem;
  }

  .show-released {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    color: var(--color-text-secondary);
  }

  .btn-primary,
  .btn-secondary {
    padding: 0.375rem 0.75rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
  }

  .btn-primary {
    background: var(--primary-color);
    color: white;
    border: none;
  }

  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-secondary {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .btn-secondary:hover {
    background: var(--button-hover);
  }

  .empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.5rem;
    padding: 2rem 1rem;
    color: var(--color-text-secondary);
    border: 1px dashed var(--border-color);
    border-radius: 6px;
  }

  .empty p {
    margin: 0;
  }

  .table-wrap {
    overflow-x: auto;
    border: 1px solid var(--color-border);
    border-radius: 4px;
  }

  .holds-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.75rem;
  }

  .holds-table th,
  .holds-table td {
    padding: 0.5rem;
    text-align: start;
    vertical-align: top;
    border-bottom: 1px solid var(--color-border);
  }

  .holds-table th {
    background: var(--color-bg-secondary);
    font-size: 0.6875rem;
    text-transform: uppercase;
    letter-spacing: 0.025em;
    color: var(--color-text-secondary);
  }

  tr.released {
    opacity: 0.7;
  }

  .reason {
    margin-top: 0.25rem;
    color: var(--color-text-secondary);
    max-width: 28rem;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .link {
    display: block;
    background: none;
    border: none;
    padding: 0;
    color: var(--primary-on-surface);
    cursor: pointer;
    font-size: 0.6875rem;
    text-decoration: underline;
  }

  .files {
    margin: 0.25rem 0 0;
    padding-inline-start: 1rem;
    max-height: 8rem;
    overflow-y: auto;
    color: var(--color-text-secondary);
  }

  .sub {
    margin-top: 0.125rem;
    color: var(--color-text-secondary);
    font-size: 0.6875rem;
  }

  .form {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    padding: 0.25rem 0;
  }

  .scope {
    border: none;
    padding: 0;
    margin: 0;
    display: flex;
    gap: 1rem;
  }

  .scope legend {
    margin-bottom: 0.25rem;
    font-weight: 600;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .field span {
    font-weight: 600;
  }

  .field small {
    color: var(--color-text-secondary);
  }

  .field small.error {
    color: var(--color-error, #dc2626);
  }

  .release-body {
    margin: 0 0 0.75rem;
  }
</style>
