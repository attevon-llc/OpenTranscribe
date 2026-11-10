<!--
  LegalOrgAcceptance.svelte — an organization administrator accepts a document on behalf
  of the organization the session acts in. The server decides who may (403
  legal_org_acceptance_forbidden) and which documents can be accepted at that scope; this
  only renders the state it returns.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import {
    OPERATOR_ACK_KEY,
    acknowledgeLegalDocument,
    type LegalDocumentSummary
  } from '$lib/api/legal';
  import { formatDateTime } from '$lib/utils/formatting';

  export let docs: LegalDocumentSummary[] = [];

  const dispatch = createEventDispatcher<{ accepted: void }>();
  let busyKey = '';
  let error: '' | 'forbidden' | 'failed' = '';

  $: orgDocs = docs.filter((d) => d.key !== OPERATOR_ACK_KEY);

  const fmt = (iso: string | null) => formatDateTime(iso);

  async function accept(doc: LegalDocumentSummary) {
    busyKey = doc.key;
    error = '';
    try {
      await acknowledgeLegalDocument(doc, 'organization');
      dispatch('accepted');
    } catch (err: unknown) {
      const code = (err as { response?: { data?: { detail?: { code?: string } } } })?.response
        ?.data?.detail?.code;
      error = code === 'legal_org_acceptance_forbidden' ? 'forbidden' : 'failed';
    } finally {
      busyKey = '';
    }
  }
</script>

<section class="org-accept" data-testid="legal-org-acceptance">
  <h4 class="org-accept-title">{$t('legal.orgAcceptance.title')}</h4>
  <p class="org-accept-note">{$t('legal.orgAcceptance.description')}</p>
  <ul class="org-accept-list">
    {#each orgDocs as doc (doc.key)}
      <li class="org-accept-row">
        <strong>{doc.title}</strong>
        {#if doc.organization_acknowledged}
          <span class="org-accept-state" data-testid="legal-org-accepted">
            {$t('legal.orgAcceptance.acceptedOn', {
              date: fmt(doc.organization_acknowledged_at)
            })}
          </span>
        {:else}
          <button
            class="btn btn-secondary"
            disabled={busyKey !== ''}
            on:click={() => accept(doc)}
            data-testid="legal-org-accept-btn"
          >
            {$t('legal.orgAcceptance.acceptForOrg')}
          </button>
        {/if}
      </li>
    {/each}
  </ul>
  {#if error === 'forbidden'}
    <p class="org-accept-error" role="alert">{$t('legal.errors.orgAcceptanceForbidden')}</p>
  {:else if error === 'failed'}
    <p class="org-accept-error" role="alert">{$t('legal.orgAcceptance.failed')}</p>
  {/if}
</section>

<style>
  .org-accept {
    margin-block-end: var(--space-4);
  }

  .org-accept-title {
    margin: 0 0 var(--space-1);
  }

  .org-accept-note {
    margin: 0 0 var(--space-2);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .org-accept-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .org-accept-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-2-5) var(--space-3-5);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background-color: var(--bg-secondary);
  }

  .org-accept-state {
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .org-accept-error {
    margin: var(--space-2) 0 0;
    color: var(--error-text);
    font-size: var(--font-size-sm);
  }
</style>
