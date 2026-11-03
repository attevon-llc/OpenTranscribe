<!--
  AuditIntegrityStatus.svelte — read-only status of audit record protection (super_admin).

  The server MEASURES what its database role can do to the protected audit tables and answers
  whether protection is enforced. This renders that answer; it never decides it.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { AdminApi, type AuditIntegrityStatus } from '$lib/api/admin';
  import { t } from '$stores/locale';

  const DOCS_URL = 'https://docs.opentranscribe.app/docs/operations/audit-integrity';

  let status: AuditIntegrityStatus | null = null;

  onMount(async () => {
    try {
      status = await AdminApi.getAuditIntegrity();
    } catch (error) {
      // Not shown to non-super_admin accounts; there is nothing to report on failure.
      console.error('Failed to load the audit record protection status:', error);
    }
  });
</script>

{#if status}
  <div class="integrity" data-testid="audit-integrity-status">
    <div class="copy">
      <span class="title">{$t('settings.audit.integrity.title')}</span>
      <p class="state" class:enforced={status.enforced} data-testid="audit-integrity-state">
        {status.enforced
          ? $t('settings.audit.integrity.enforced')
          : $t('settings.audit.integrity.notEnforced')}
      </p>
      <a href={DOCS_URL} target="_blank" rel="noopener noreferrer" class="docs">
        {$t('settings.audit.integrity.docsLink')}
      </a>
    </div>
  </div>
{/if}

<style>
  .integrity {
    padding: 0.75rem;
    margin-bottom: 0.75rem;
    background: var(--color-bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
  }

  .title {
    font-weight: 600;
    color: var(--text-color);
  }

  .state {
    margin: 0.25rem 0 0;
    font-size: 0.75rem;
    line-height: 1.4;
    color: var(--text-secondary);
  }

  .state.enforced {
    color: var(--text-color);
  }

  .docs {
    display: inline-block;
    margin-top: 0.25rem;
    font-size: 0.75rem;
    color: var(--primary-on-surface);
  }
</style>
