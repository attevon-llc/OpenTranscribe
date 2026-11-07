<!--
  LegalHoldsPanel.svelte - the legal holds of one authority: an organization's (its
  administrators, `scope="organization"`, issue #1276) or the operator's (instance
  administrators, `scope="platform"`, legal-hold plan §8.4). One component for both: only the API
  and the copy differ.

  List, place and release. The server decides everything that matters (which files a hold may
  name, whether a reason is long enough, what is still held after a release); this renders it,
  sends the request and shows the answer. Placing goes through the shared PlaceHoldDialog. The
  reason is shown here and nowhere else: the badge on a file or collection says only that it is
  held and by whom. Release lives here only.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Badge from '$components/ui/Badge.svelte';
  import { formatDate } from '$lib/utils/formatting';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import HoldExplainer from '$components/legal-hold/HoldExplainer.svelte';
  import PlaceHoldDialog from '$components/legal-hold/PlaceHoldDialog.svelte';
  import type { OrgHeldFile, OrgHold } from '$lib/api/orgAdmin';
  import {
    HOLD_REASON_MAX,
    HOLD_REASON_MIN,
    holdReasonValid,
    type HoldPlacer,
  } from '$lib/legalHold/authority';
  import { holdsApiFor } from '$lib/legalHold/holdsApi';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { t, locale } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  /** Whose holds: the caller's organization, or the operator's. */
  export let scope: HoldPlacer = 'organization';

  $: api = holdsApiFor(scope);
  $: descriptionKey = scope === 'platform' ? 'holds.platform.description' : 'holds.description';
  $: reasonHelpKey = scope === 'platform' ? 'holds.platform.reasonHelp' : 'holds.reasonHelp';

  let holds: OrgHold[] = [];
  let total = 0;
  let loading = true;
  let showReleased = false;
  let placing = false;

  /** Files of the holds the administrator expanded, by hold uuid. */
  let expanded: Record<string, OrgHeldFile[]> = {};

  let releasing: OrgHold | null = null;
  let releaseReason = '';
  let saving = false;

  $: releaseReasonValid = holdReasonValid(releaseReason);

  async function load() {
    loading = true;
    try {
      const result = await api.listHolds({ active_only: !showReleased, limit: 200 });
      holds = result.holds;
      total = result.total;
    } catch (error) {
      console.error('Failed to load legal holds:', error);
      toastStore.error($t('holds.loadError'));
    } finally {
      loading = false;
    }
  }

  async function toggleFiles(hold: OrgHold) {
    if (expanded[hold.uuid]) {
      const { [hold.uuid]: _closed, ...rest } = expanded;
      expanded = rest;
      return;
    }
    try {
      const detail = await api.getHold(hold.uuid);
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
      await api.releaseHold(releasing.uuid, releaseReason.trim());
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

  onMount(load);
</script>

<div class="holds" data-testid="legal-holds-panel" data-scope={scope}>
  <p class="intro">{$t(descriptionKey)}</p>
  {#if scope === 'platform'}
    <p class="intro note">{$t('holds.platform.takedownNote')}</p>
  {/if}
  <HoldExplainer />

  <div class="toolbar">
    <button class="btn btn-primary" type="button" on:click={() => (placing = true)}>{$t('holds.place')}</button>
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
                {formatDate(hold.placed_at, $locale)}
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
                  <div class="sub">{$t('holds.releasedAt', { date: formatDate(hold.released_at, $locale) })}</div>
                {/if}
              </td>
              <td>
                {#if hold.active}
                  <button class="btn btn-secondary" type="button" on:click={() => openRelease(hold)}>
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

<PlaceHoldDialog
  isOpen={placing}
  placer={scope}
  on:placed={load}
  on:close={() => (placing = false)}
/>

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
      maxlength={HOLD_REASON_MAX}
      bind:value={releaseReason}
      aria-invalid={releaseReason.trim() !== '' && !releaseReasonValid}
    ></textarea>
    <small>{$t(reasonHelpKey, { min: HOLD_REASON_MIN, max: HOLD_REASON_MAX })}</small>
  </label>
  <svelte:fragment slot="footer">
    <button class="btn btn-secondary" type="button" on:click={() => (releasing = null)}>
      {$t('common.cancel')}
    </button>
    <button
      class="btn btn-primary"
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
    padding: var(--space-2);
    font-size: var(--font-size-sm);
  }

  .intro {
    margin: 0 0 var(--space-3);
    color: var(--color-text-secondary);
  }

  .intro.note {
    font-style: italic;
  }

  .toolbar {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    margin-block: var(--space-3);
  }

  .show-released {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
    color: var(--color-text-secondary);
  }

  .empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-8) var(--space-4);
    color: var(--color-text-secondary);
    border: 1px dashed var(--border-color);
    border-radius: var(--radius-md);
  }

  .empty p {
    margin: 0;
  }

  .table-wrap {
    overflow-x: auto;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-sm);
  }

  .holds-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-xs);
  }

  .holds-table th,
  .holds-table td {
    padding: var(--space-2);
    text-align: start;
    vertical-align: top;
    border-block-end: 1px solid var(--color-border);
  }

  .holds-table th {
    background: var(--color-bg-secondary);
    font-size: var(--font-size-2xs);
    text-transform: uppercase;
    letter-spacing: 0.025em;
    color: var(--color-text-secondary);
  }

  tr.released {
    opacity: 0.7;
  }

  .reason {
    margin-block-start: var(--space-1);
    color: var(--color-text-secondary);
    max-width: 28rem;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .link {
    display: block;
    background: none;
    border: none;
    box-shadow: none;
    padding: 0;
    color: var(--primary-on-surface);
    cursor: pointer;
    font-size: var(--font-size-2xs);
    text-decoration: underline;
  }

  .files {
    margin: var(--space-1) 0 0;
    padding-inline-start: var(--space-4);
    max-height: 8rem;
    overflow-y: auto;
    color: var(--color-text-secondary);
  }

  .sub {
    margin-block-start: var(--space-0-5);
    color: var(--color-text-secondary);
    font-size: var(--font-size-2xs);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .field span {
    font-weight: var(--font-weight-semibold);
  }

  .field small {
    color: var(--color-text-secondary);
  }

  .release-body {
    margin: 0 0 var(--space-3);
  }
</style>
