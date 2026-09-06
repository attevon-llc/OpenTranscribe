<script lang="ts">
  /**
   * The deployment-wide email-notification configs (super_admin tier).
   *
   * These are the mailers themselves — creating one holds SMTP/Graph credentials,
   * which is why this block is super_admin. Attaching a mailer to a particular
   * source is a different, owner-level right and lives in
   * `WatchSourceEmailLinksModal.svelte`.
   *
   * Presentational: the coordinator owns the list and every call.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { EmailConfig } from '$lib/api/watchSourcesApi';

  export let configs: EmailConfig[] = [];
  export let showHelp = false;

  const dispatch = createEventDispatcher<{
    create: void;
    edit: EmailConfig;
    test: EmailConfig;
    delete: EmailConfig;
    toggleHelp: void;
  }>();
</script>

<div class="section-head admin-section">
  <div class="email-heading">
    <h4>{$t('settings.emailNotifications.heading')}</h4>
    <span class="experimental-badge" title={$t('settings.emailNotifications.experimentalNote')}>
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M9 3h6M10 3v6.5L5.2 18a2 2 0 0 0 1.8 3h10a2 2 0 0 0 1.8-3L14 9.5V3" />
        <path d="M7.5 14h9" />
      </svg>
      {$t('settings.emailNotifications.experimental')}
    </span>
    <button
      class="info-icon"
      type="button"
      aria-expanded={showHelp}
      aria-label={$t('settings.emailNotifications.setupTitle')}
      title={$t('settings.emailNotifications.setupTitle')}
      on:click={() => dispatch('toggleHelp')}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="16" x2="12" y2="12" />
        <line x1="12" y1="8" x2="12.01" y2="8" />
      </svg>
    </button>
  </div>
  <button class="btn btn-primary" on:click={() => dispatch('create')}>
    + {$t('settings.emailNotifications.addConfig')}
  </button>
</div>
<p class="experimental-note">{$t('settings.emailNotifications.experimentalNote')}</p>
{#if showHelp}
  <div class="email-help">
    <h5>{$t('settings.emailNotifications.setupTitle')}</h5>
    <p>
      <strong>SMTP (Gmail, Outlook, Yahoo)</strong> — {$t('settings.emailNotifications.smtpHelp')}
    </p>
    <p><strong>Microsoft 365</strong> — {$t('settings.emailNotifications.m365Help')}</p>
    <p><strong>Exchange</strong> — {$t('settings.emailNotifications.exchangeHelp')}</p>
  </div>
{/if}
{#if configs.length === 0}
  <p class="muted">{$t('settings.emailNotifications.empty')}</p>
{:else}
  <div class="email-list">
    {#each configs as c (c.uuid)}
      <div class="email-card">
        <div>
          <span class="badge type-badge">{c.provider.toUpperCase()}</span>
          <span class="email-name">{c.name}</span>
          <span class="email-from">{c.from_address}</span>
          {#if c.test_status}
            <span class="badge {c.test_status === 'success' ? 'badge-success' : 'badge-error'}">
              {c.test_status}
            </span>
          {/if}
          {#if c.linked_source_count}
            <!-- Deleting this config cascades its links away, so the count is the
                 only warning an admin gets before those sources go quiet. -->
            <span class="badge linked-badge">
              {$t('settings.emailNotifications.links.usedBySources', {
                count: c.linked_source_count,
              })}
            </span>
          {/if}
        </div>
        <div class="email-actions">
          <button class="btn btn-secondary btn-sm" on:click={() => dispatch('test', c)}>
            {$t('settings.emailNotifications.test')}
          </button>
          <button class="btn btn-secondary btn-sm" on:click={() => dispatch('edit', c)}>
            {$t('common.edit')}
          </button>
          <button class="btn btn-danger btn-sm" on:click={() => dispatch('delete', c)}>
            {$t('common.delete')}
          </button>
        </div>
      </div>
    {/each}
  </div>
{/if}

<style>
  .section-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: 8px 0 12px;
  }
  .section-head h4 {
    margin: 0;
  }
  .admin-section {
    margin-top: 28px;
    border-top: 1px solid var(--border-color);
    padding-top: 16px;
  }
  .email-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .email-card {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 12px 14px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
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
  .badge-success {
    background: rgba(34, 197, 94, 0.15);
    color: var(--success-color, #16a34a);
  }
  .badge-error {
    background: rgba(239, 68, 68, 0.15);
    color: var(--error-color, #dc2626);
  }
  .linked-badge {
    background: rgba(99, 102, 241, 0.15);
    color: var(--primary-on-surface);
    margin-left: 6px;
  }
  .email-name {
    font-weight: 600;
    font-size: 0.95rem;
  }
  .email-from {
    color: var(--text-secondary);
    font-size: 0.85rem;
    margin-left: 8px;
  }
  .email-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .btn-sm {
    padding: 4px 10px;
    font-size: 0.8rem;
  }
  .muted {
    color: var(--text-secondary);
    font-size: 0.85rem;
  }
  .email-heading {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .email-heading h4 {
    margin: 0;
  }
  .experimental-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 0.68rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    padding: 2px 8px;
    border-radius: 10px;
    background: rgba(234, 179, 8, 0.15);
    color: #b45309;
  }
  :global([data-theme='dark']) .experimental-badge {
    color: #fbbf24;
  }
  .info-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: none;
    box-shadow: none;
    padding: 2px;
    cursor: pointer;
    color: var(--text-secondary);
    border-radius: 50%;
  }
  .info-icon:hover {
    color: var(--primary-on-surface);
    background: var(--button-hover);
    transform: none;
    box-shadow: none;
  }
  .experimental-note {
    font-size: 0.8rem;
    color: var(--text-secondary);
    margin: 0 0 10px;
  }
  .email-help {
    border: 1px solid var(--border-color);
    border-left: 3px solid var(--primary-color);
    border-radius: 6px;
    background: var(--button-hover);
    padding: 10px 14px;
    margin-bottom: 12px;
  }
  .email-help h5 {
    margin: 0 0 6px;
    font-size: 0.85rem;
  }
  .email-help p {
    margin: 4px 0;
    font-size: 0.8rem;
    color: var(--text-secondary);
    line-height: 1.5;
  }
</style>
