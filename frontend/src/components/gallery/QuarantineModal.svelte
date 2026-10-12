<script lang="ts">
  /**
   * Admin takedown (abuse/DMCA) for the gallery selection (issue #576).
   *
   * A client-side SEQUENTIAL loop, one `POST .../quarantine` per file — not a
   * new bulk endpoint (§B.5 J-B2): the existing bulk-action route is gated on
   * ownership, not the platform-admin gate this action needs, and a sequential
   * loop keeps one audit event per file with its own reason/IP/user-agent.
   * Sequential (never `Promise.all`) because a takedown is a legally
   * significant action and ordering matters in an audit log.
   *
   * §B.2's corollary — this modal is deliberately narrow and cannot reach a
   * GDPR erasure path: it calls only `AdminApi.quarantineFile`, names no user
   * (only files), and offers no destination beyond quarantine + an optional
   * legal hold. See `admin.gdpr.absence.test.ts` for the standing guard.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { AdminApi } from '$lib/api/admin';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import type { MediaFile } from '$lib/types/media';

  export let isOpen = false;
  export let files: MediaFile[] = [];

  const dispatch = createEventDispatcher<{ close: void; quarantined: { uuids: string[] } }>();

  const REASON_MAX_LENGTH = 2000;
  const NAMES_PREVIEW_COUNT = 5;

  let reason = '';
  let legalHold = true;
  let submitting = false;

  // Matches the owner notice's precedence (title || filename) so the admin
  // sees the exact string the owner will read.
  $: displayNames = files.map((f) => f.title || f.filename || f.uuid);
  $: previewNames = displayNames.slice(0, NAMES_PREVIEW_COUNT);
  $: extraCount = Math.max(0, displayNames.length - NAMES_PREVIEW_COUNT);
  $: reasonTrimmed = reason.trim();
  $: canSubmit = reasonTrimmed.length > 0 && files.length > 0 && !submitting;

  function handleClose() {
    if (submitting) return;
    reason = '';
    legalHold = true;
    dispatch('close');
  }

  async function handleSubmit() {
    if (!canSubmit) return;
    submitting = true;

    const succeededUuids: string[] = [];
    let failedCount = 0;

    // Sequential, deliberately — see the module docstring.
    for (const file of files) {
      try {
        await AdminApi.quarantineFile(file.uuid, reasonTrimmed, legalHold);
        succeededUuids.push(file.uuid);
      } catch (err) {
        failedCount += 1;
        // eslint-disable-next-line no-console -- one failed file must not read as a failed batch
        console.error(`Quarantine failed for ${file.uuid}:`, getErrorMessage(err));
      }
    }

    submitting = false;

    if (succeededUuids.length > 0) {
      toastStore.success($t('gallery.quarantine.succeeded', { count: succeededUuids.length }));
    }
    if (failedCount > 0) {
      toastStore.error($t('gallery.quarantine.failed', { count: failedCount }));
    }

    if (succeededUuids.length > 0) {
      dispatch('quarantined', { uuids: succeededUuids });
    }
    reason = '';
    legalHold = true;
    dispatch('close');
  }
</script>

<BaseModal {isOpen} title={$t('gallery.quarantine.title')} onClose={handleClose} maxWidth="520px">
  <div class="quarantine-modal-body">
    <div class="file-list">
      <p class="file-list-label">
        {$t('gallery.quarantine.filesLabel', { count: displayNames.length })}
      </p>
      <ul>
        {#each previewNames as name (name)}
          <li>{name}</li>
        {/each}
        {#if extraCount > 0}
          <li class="more">{$t('gallery.quarantine.moreFiles', { count: extraCount })}</li>
        {/if}
      </ul>
    </div>

    <label class="field" for="quarantine-reason">
      {$t('gallery.quarantine.reasonLabel')}
      <textarea
        id="quarantine-reason"
        bind:value={reason}
        maxlength={REASON_MAX_LENGTH}
        rows="4"
        placeholder={$t('gallery.quarantine.reasonPlaceholder')}
        disabled={submitting}
      ></textarea>
      <span class="char-count">{reason.length} / {REASON_MAX_LENGTH}</span>
    </label>

    <label class="legal-hold-field">
      <input type="checkbox" bind:checked={legalHold} disabled={submitting} />
      <span>
        <strong>{$t('gallery.quarantine.legalHoldLabel')}</strong>
        <br />
        {$t('gallery.quarantine.legalHoldHelp')}
      </span>
    </label>

    <p class="notice">{$t('gallery.quarantine.notice')}</p>
    <p class="scope-notice">{$t('gallery.quarantine.scopeNotice')}</p>
  </div>

  <svelte:fragment slot="footer">
    <button class="btn-secondary" on:click={handleClose} disabled={submitting}>
      {$t('common.cancel')}
    </button>
    <button class="btn-destructive" on:click={handleSubmit} disabled={!canSubmit}>
      {#if submitting}
        <Spinner size="small" />
      {/if}
      {$t('gallery.quarantine.submit', { count: displayNames.length })}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .quarantine-modal-body {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .file-list-label {
    margin: 0 0 0.375rem;
    font-weight: 600;
    font-size: 0.875rem;
  }

  .file-list ul {
    margin: 0;
    padding-left: 1.25rem;
    font-size: 0.8125rem;
    color: var(--text-secondary, var(--text-color));
    max-height: 120px;
    overflow-y: auto;
  }

  .file-list .more {
    font-style: italic;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
    font-weight: 600;
    font-size: 0.875rem;
  }

  textarea {
    resize: vertical;
    min-height: 80px;
    padding: 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--input-bg, var(--bg-color));
    color: var(--text-color);
    font-family: inherit;
    font-size: 0.8125rem;
    font-weight: normal;
  }

  .char-count {
    align-self: flex-end;
    font-size: 0.75rem;
    font-weight: normal;
    color: var(--text-secondary, var(--text-color));
  }

  .legal-hold-field {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    font-size: 0.8125rem;
    color: var(--text-secondary, var(--text-color));
  }

  .legal-hold-field input {
    margin-top: 0.2rem;
  }

  .notice,
  .scope-notice {
    margin: 0;
    font-size: 0.75rem;
    color: var(--text-secondary, var(--text-color));
  }

  .btn-secondary {
    padding: 0.5rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--bg-color);
    color: var(--text-color);
    cursor: pointer;
    font-size: 0.875rem;
  }

  .btn-secondary:hover:not(:disabled) {
    background: var(--hover-color, rgba(0, 0, 0, 0.05));
  }

  .btn-destructive {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.5rem 1rem;
    border: none;
    border-radius: 6px;
    background: #dc2626;
    color: white;
    cursor: pointer;
    font-size: 0.875rem;
  }

  .btn-destructive:hover:not(:disabled) {
    background: #b91c1c;
  }

  .btn-destructive:disabled,
  .btn-secondary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
