<script lang="ts">
  import Badge from '$components/ui/Badge.svelte';
  import ProgressBar from '$components/ui/ProgressBar.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import PrivacySwitchCard from '$components/privacy/PrivacySwitchCard.svelte';
  import { t } from '$stores/locale';

  // Every shared styling primitive in every state, on one deterministic page, so the styling
  // A/B tool (backend/tests/e2e/styling_ab.py) and the axe scan (test_a11y.py, surface
  // `ui-kit`) can photograph live-progress, busy and error states that a fresh stack cannot
  // otherwise be made to show on demand. docs/design/frontend_styling_layer_plan.md §5.7.
  //
  // Rules for editing this page: no backend calls, no dates, no random values, labels only
  // from EXISTING locale keys (so the `ar` capture shows real RTL text), and each section
  // keeps its `data-kit-section` name because the tool's manifest refers to it. Component
  // lanes extend the section they change; they do not rename or reorder.
  //
  // Reachable only when authenticated (the root layout gates every route), same as
  // `a11y-fixtures/status-badges`.
  const noop = () => {};
  const buttonVariants = ['btn-primary', 'btn-secondary', 'btn-danger', 'btn-warning'] as const;
  const badgeVariants = ['default', 'success', 'warning', 'error', 'info'] as const;
  const tintFamilies = [
    { name: 'error', bg: 'var(--danger-bg)', text: 'var(--color-error-text)' },
    { name: 'warning', bg: 'var(--warning-bg)', text: 'var(--warning-text)' },
    { name: 'success', bg: 'var(--success-bg)', text: 'var(--color-success-text)' },
    { name: 'info', bg: 'var(--color-info-bg)', text: 'var(--color-info-text)' },
  ] as const;

  let confirmOpen = false;
  let confirmClass = 'modal-delete-button';

  function openConfirm(cls: string) {
    confirmClass = cls;
    confirmOpen = true;
  }
</script>

<div class="kit-page">
  <section data-kit-section="buttons" aria-labelledby="kit-buttons">
    <h2 id="kit-buttons">{$t('common.actions')}</h2>
    {#each buttonVariants as variant (variant)}
      <div class="kit-row">
        <button type="button" class="btn {variant}">{$t('common.save')}</button>
        <button type="button" class="btn {variant} btn-small">{$t('common.save')}</button>
        <button type="button" class="btn {variant}" disabled>{$t('common.save')}</button>
        <button type="button" class="btn {variant}" aria-busy="true" aria-disabled="true">
          <Spinner size="small" />
          {$t('common.saving')}
        </button>
      </div>
    {/each}
    <div class="kit-row">
      <button type="button" class="modal-button modal-cancel-button">{$t('common.cancel')}</button>
      <button type="button" class="modal-button modal-primary-button">{$t('common.save')}</button>
      <button type="button" class="modal-button modal-delete-button">{$t('common.delete')}</button>
      <button type="button" class="modal-button modal-warning-button">{$t('common.apply')}</button>
    </div>
  </section>

  <section data-kit-section="toggles" aria-labelledby="kit-toggles">
    <h2 id="kit-toggles">{$t('common.enabled')} / {$t('common.disabled')}</h2>
    <PrivacySwitchCard
      id="kit-toggle-off"
      title={$t('common.disabled')}
      description={$t('common.status')}
      checked={false}
      onToggle={noop}
    />
    <PrivacySwitchCard
      id="kit-toggle-on"
      title={$t('common.enabled')}
      description={$t('common.status')}
      checked={true}
      onToggle={noop}
    />
    <PrivacySwitchCard
      id="kit-toggle-locked"
      title={$t('common.quarantined')}
      description={$t('common.status')}
      checked={true}
      locked={true}
      lockedReason={$t('common.unknownError')}
      onToggle={noop}
    />
    <PrivacySwitchCard
      id="kit-toggle-busy"
      title={$t('common.processing')}
      description={$t('common.status')}
      checked={false}
      busy={true}
      onToggle={noop}
    />
  </section>

  <section data-kit-section="form-fields" aria-labelledby="kit-fields">
    <h2 id="kit-fields">{$t('common.name')}</h2>
    <div class="kit-field">
      <label for="kit-input">{$t('common.name')}</label>
      <input id="kit-input" type="text" />
    </div>
    <div class="kit-field">
      <label for="kit-input-hint">{$t('common.name')}</label>
      <input id="kit-input-hint" type="text" aria-describedby="kit-input-hint-text" />
      <p id="kit-input-hint-text" class="kit-hint">{$t('common.moreInformation')}</p>
    </div>
    <div class="kit-field">
      <label for="kit-input-error">{$t('common.name')}</label>
      <input
        id="kit-input-error"
        type="text"
        aria-invalid="true"
        aria-describedby="kit-input-error-text"
      />
      <p id="kit-input-error-text" class="kit-error" role="alert">{$t('common.unknownError')}</p>
    </div>
    <div class="kit-field">
      <label for="kit-select">{$t('common.type')}</label>
      <select id="kit-select">
        <option>{$t('common.audio')}</option>
        <option>{$t('common.video')}</option>
      </select>
    </div>
    <div class="kit-field">
      <label for="kit-textarea">{$t('common.date')}</label>
      <textarea id="kit-textarea" rows="3"></textarea>
    </div>
  </section>

  <section data-kit-section="progress" aria-labelledby="kit-progress">
    <h2 id="kit-progress">{$t('common.processing')}</h2>
    <ProgressBar percent={0} label={$t('common.pending')} />
    <ProgressBar percent={42} label={$t('common.processing')} />
    <ProgressBar percent={100} label={$t('common.completed')} />
    <ProgressBar percent={null} label={$t('common.loading')} />
    <div class="kit-row">
      <Spinner size="small" />
      <Spinner size="medium" />
      <Spinner size="large" />
    </div>
  </section>

  <section data-kit-section="badges" aria-labelledby="kit-badges">
    <h2 id="kit-badges">{$t('common.status')}</h2>
    <div class="kit-row">
      {#each badgeVariants as variant (variant)}
        <Badge {variant}>{$t('common.status')}</Badge>
      {/each}
    </div>
  </section>

  <section data-kit-section="banners" aria-labelledby="kit-banners">
    <h2 id="kit-banners">{$t('common.moreInformation')}</h2>
    <!-- No shared banner exists yet (lane 2.6 adds ui/Banner and fills this section). -->
  </section>

  <section data-kit-section="status-text" aria-labelledby="kit-status-text">
    <h2 id="kit-status-text">{$t('common.status')}</h2>
    {#each tintFamilies as family (family.name)}
      <p class="kit-tint" style="background: {family.bg}; color: {family.text}">
        {$t('common.status')} ({family.name})
      </p>
    {/each}
  </section>

  <section data-kit-section="confirm" aria-labelledby="kit-confirm">
    <h2 id="kit-confirm">{$t('modal.confirmAction')}</h2>
    <!-- Triggers only: the A/B tool opens the dialog in its capture step and never confirms. -->
    <div class="kit-row">
      <button
        type="button"
        class="btn btn-danger"
        data-kit-trigger="confirm-danger"
        on:click={() => openConfirm('modal-delete-button')}
      >
        {$t('common.delete')}
      </button>
      <button
        type="button"
        class="btn btn-warning"
        data-kit-trigger="confirm-warning"
        on:click={() => openConfirm('modal-warning-button')}
      >
        {$t('common.apply')}
      </button>
      <button
        type="button"
        class="btn btn-primary"
        data-kit-trigger="confirm-primary"
        on:click={() => openConfirm('confirm-button')}
      >
        {$t('common.confirm')}
      </button>
    </div>
  </section>
</div>

<ConfirmationModal
  bind:isOpen={confirmOpen}
  confirmButtonClass={confirmClass}
  on:confirm={noop}
  on:cancel={noop}
/>

<style>
  .kit-page {
    width: 100%;
    max-width: 60rem;
    padding: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
  }

  .kit-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    margin-block-end: var(--space-2);
  }

  .kit-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
    max-width: 24rem;
    margin-block-end: var(--space-3);
  }

  .kit-hint {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
  }

  .kit-error {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--color-error-text);
  }

  .kit-tint {
    margin: 0 0 var(--space-2);
    padding: var(--space-2) var(--space-3);
    border-radius: 6px;
  }
</style>
