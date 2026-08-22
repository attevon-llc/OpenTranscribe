<script lang="ts">
  /**
   * Attach email configs to ONE watch source, with per-link options (#490).
   *
   * Visible to the **source owner**, not gated on `isSuperAdmin`: linking is
   * owner-level on the backend. The super_admin gate belongs on the config-CRUD block
   * (`EmailConfigList`), which is a different right — holding a mailer's credentials
   * versus subscribing your own source to one that already exists.
   *
   * Three states look configured and deliver nothing, so each is warned about
   * inline: both notify flags off, a disabled config, and no recipients anywhere.
   * `send_notification` skips all three, and the first two are invisible from the
   * link alone.
   */
  import { createEventDispatcher } from 'svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import {
    getEmailLinks,
    getAvailableEmailConfigs,
    linkEmailConfig,
    unlinkEmailConfig,
    type WatchSource,
    type EmailLink,
    type EmailConfigOption,
  } from '$lib/api/watchSourcesApi';

  export let show = false;
  export let source: WatchSource | null = null;

  const dispatch = createEventDispatcher<{ close: void }>();

  let links: EmailLink[] = [];
  let available: EmailConfigOption[] = [];
  let loading = false;
  let savingUuid: string | null = null;
  let pickedUuid = '';
  let loadedFor: string | null = null;

  $: if (show && source && loadedFor !== source.uuid) {
    loadedFor = source.uuid;
    pickedUuid = '';
    load();
  }
  $: if (!show) loadedFor = null;

  async function load() {
    if (!source) return;
    loading = true;
    try {
      const [linkRows, options] = await Promise.all([
        getEmailLinks(source.uuid),
        getAvailableEmailConfigs(source.uuid),
      ]);
      links = linkRows;
      available = options;
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.emailNotifications.links.loadFailed')));
    } finally {
      loading = false;
    }
  }

  /** Mirrors the backend validator so the user sees the problem before the round trip. */
  function invalidRecipients(csv: string | null | undefined): boolean {
    if (!csv || !csv.trim()) return false;
    return csv.split(',').some((part) => {
      const candidate = part.trim();
      if (!candidate) return false;
      const [local, domain] = candidate.split('@');
      return !local || !domain || !domain.includes('.');
    });
  }

  /**
   * Read from the LINK, not the picker: the picker excludes configs already linked,
   * so keying these off it meant the two config-side warnings could never fire for
   * an existing link — the exact rows they exist for.
   */
  function warningsFor(link: EmailLink): string[] {
    const warnings: string[] = [];
    if (!link.notify_on_success && !link.notify_on_error) {
      warnings.push($t('settings.emailNotifications.links.warnNoEvents'));
    }
    if (!link.config_is_enabled) {
      warnings.push($t('settings.emailNotifications.links.warnConfigDisabled'));
    }
    if (
      !link.config_has_default_recipients &&
      !(link.additional_recipients && link.additional_recipients.trim())
    ) {
      warnings.push($t('settings.emailNotifications.links.warnNoRecipients'));
    }
    return warnings;
  }

  async function save(link: EmailLink) {
    if (!source) return;
    if (invalidRecipients(link.additional_recipients)) {
      toastStore.error($t('settings.emailNotifications.links.invalidRecipients'));
      return;
    }
    savingUuid = link.email_config_uuid;
    try {
      // The backend POST is an upsert, so creating and editing are one call.
      await linkEmailConfig(source.uuid, {
        email_config_uuid: link.email_config_uuid,
        additional_recipients: link.additional_recipients ?? null,
        notify_on_success: link.notify_on_success,
        notify_on_error: link.notify_on_error,
      });
      toastStore.success($t('settings.emailNotifications.links.saved'));
      await load();
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.emailNotifications.links.saveFailed')));
    } finally {
      savingUuid = null;
    }
  }

  async function addPicked() {
    if (!source || !pickedUuid) return;
    savingUuid = pickedUuid;
    try {
      await linkEmailConfig(source.uuid, { email_config_uuid: pickedUuid });
      toastStore.success($t('settings.emailNotifications.links.linked'));
      pickedUuid = '';
      await load();
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.emailNotifications.links.saveFailed')));
    } finally {
      savingUuid = null;
    }
  }

  async function unlink(link: EmailLink) {
    if (!source) return;
    savingUuid = link.email_config_uuid;
    try {
      await unlinkEmailConfig(source.uuid, link.email_config_uuid);
      toastStore.success($t('settings.emailNotifications.links.unlinked'));
      await load();
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.emailNotifications.links.saveFailed')));
    } finally {
      savingUuid = null;
    }
  }
</script>

<BaseModal isOpen={show} onClose={() => dispatch('close')} maxWidth="680px">
  <svelte:fragment slot="header">
    <h2 class="modal-title">
      {$t('settings.emailNotifications.links.title', { name: source?.name ?? '' })}
    </h2>
  </svelte:fragment>

  <div class="links-body">
    <p class="scope-note">{$t('settings.emailNotifications.links.perScanNote')}</p>

    {#if loading}
      <div class="links-loading"><Spinner /></div>
    {:else}
      {#if links.length === 0}
        <EmptyState
          title={$t('settings.emailNotifications.links.emptyTitle')}
          description={$t('settings.emailNotifications.links.emptyDescription')}
          padding="24px 16px"
        />
      {:else}
        <div class="link-list">
          {#each links as link (link.email_config_uuid)}
            <div class="link-card">
              <div class="link-head">
                <span class="link-name">{link.email_config_name}</span>
                <span class="badge type-badge">{link.email_config_provider.toUpperCase()}</span>
                <button
                  class="btn btn-danger btn-sm unlink"
                  disabled={savingUuid === link.email_config_uuid}
                  on:click={() => unlink(link)}
                >
                  {$t('settings.emailNotifications.links.unlink')}
                </button>
              </div>

              <div class="link-options">
                <label class="checkbox-row">
                  <input type="checkbox" bind:checked={link.notify_on_success} />
                  <span>{$t('settings.emailNotifications.links.notifyOnSuccess')}</span>
                </label>
                <label class="checkbox-row">
                  <input type="checkbox" bind:checked={link.notify_on_error} />
                  <span>{$t('settings.emailNotifications.links.notifyOnError')}</span>
                </label>
              </div>

              <div class="form-group">
                <label for={`recipients-${link.email_config_uuid}`}>
                  {$t('settings.emailNotifications.links.additionalRecipients')}
                </label>
                <input
                  id={`recipients-${link.email_config_uuid}`}
                  class="form-input"
                  type="text"
                  bind:value={link.additional_recipients}
                  placeholder={$t('settings.emailNotifications.links.recipientsPlaceholder')}
                />
                <small class="form-hint">
                  {$t('settings.emailNotifications.links.recipientsHint')}
                </small>
              </div>

              {#each warningsFor(link) as warning (warning)}
                <div class="link-warning">{warning}</div>
              {/each}

              <div class="link-actions">
                <button
                  class="btn btn-primary btn-sm"
                  disabled={savingUuid === link.email_config_uuid}
                  on:click={() => save(link)}
                >
                  {savingUuid === link.email_config_uuid ? $t('common.saving') : $t('common.save')}
                </button>
              </div>
            </div>
          {/each}
        </div>
      {/if}

      <div class="add-row">
        {#if available.length > 0}
          <select
            class="form-input"
            bind:value={pickedUuid}
            aria-label={$t('settings.emailNotifications.links.addLabel')}
          >
            <option value="">{$t('settings.emailNotifications.links.addLabel')}</option>
            {#each available as option (option.uuid)}
              <option value={option.uuid}>{option.name}</option>
            {/each}
          </select>
          <button class="btn btn-primary" disabled={!pickedUuid} on:click={addPicked}>
            {$t('settings.emailNotifications.links.add')}
          </button>
        {:else if links.length === 0}
          <!-- An owner cannot create a config, so an empty picker would read as a
               bug rather than as "somebody else has to do this first". -->
          <p class="muted">{$t('settings.emailNotifications.links.noConfigsExist')}</p>
        {:else}
          <p class="muted">{$t('settings.emailNotifications.links.allLinked')}</p>
        {/if}
      </div>
    {/if}
  </div>

  <svelte:fragment slot="footer">
    <button class="btn btn-secondary" on:click={() => dispatch('close')}>
      {$t('common.close')}
    </button>
  </svelte:fragment>
</BaseModal>

<style>
  .links-body {
    min-height: 180px;
  }
  .scope-note {
    margin: 0 0 12px;
    font-size: 0.8rem;
    color: var(--text-secondary);
    font-style: italic;
  }
  .links-loading {
    display: flex;
    justify-content: center;
    padding: 32px;
  }
  .link-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .link-card {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    padding: 12px 14px;
  }
  .link-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }
  .link-name {
    font-weight: 600;
    font-size: 0.95rem;
  }
  .unlink {
    margin-left: auto;
  }
  .badge {
    font-size: 0.7rem;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
  }
  .type-badge {
    background: var(--button-hover);
    color: var(--text-secondary);
  }
  .link-options {
    display: flex;
    gap: 16px;
    margin-bottom: 8px;
    flex-wrap: wrap;
  }
  .checkbox-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.9rem;
    cursor: pointer;
  }
  /* Override the global `input { width:100% }` base so checkboxes stay square. */
  .checkbox-row input[type='checkbox'] {
    width: 16px;
    height: 16px;
    min-height: 0;
    margin: 0;
    padding: 0;
    flex: none;
    cursor: pointer;
    accent-color: var(--primary-color);
  }
  .form-group {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .form-group label {
    font-size: 0.85rem;
    color: var(--text-secondary);
  }
  .form-hint {
    font-size: 0.8rem;
    color: var(--text-secondary);
    font-style: italic;
  }
  .link-warning {
    margin-top: 8px;
    padding: 6px 10px;
    border-radius: 6px;
    border-left: 3px solid var(--warning-color);
    background: rgba(234, 179, 8, 0.1);
    font-size: 0.8rem;
    color: var(--text-secondary);
  }
  .link-actions {
    display: flex;
    justify-content: flex-end;
    margin-top: 10px;
  }
  .add-row {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-top: 16px;
    padding-top: 12px;
    border-top: 1px solid var(--border-color);
  }
  .muted {
    color: var(--text-secondary);
    font-size: 0.85rem;
    margin: 0;
  }
  .btn-sm {
    padding: 4px 10px;
    font-size: 0.8rem;
  }
  .modal-title {
    margin: 0;
    font-size: 1.1rem;
  }
</style>
