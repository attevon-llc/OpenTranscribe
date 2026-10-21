<!--
  LegalSettings.svelte — read-only list of the versioned legal documents, the
  operator's own acknowledgment state, and (admin+) the acknowledgment record.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import LegalDocumentViewer from '$components/legal/LegalDocumentViewer.svelte';
  import {
    OPERATOR_ACK_KEY,
    listLegalDocuments,
    listLegalAcknowledgments,
    type LegalDocumentSummary,
    type LegalAcknowledgment
  } from '$lib/api/legal';

  export let isAdmin = false;
  export let isSuperAdmin = false;

  let docs: LegalDocumentSummary[] = [];
  let acks: LegalAcknowledgment[] = [];
  let viewerKey = '';
  let viewerOpen = false;

  $: operatorDoc = docs.find((d) => d.key === OPERATOR_ACK_KEY);

  onMount(() => {
    (async () => {
      try {
        docs = await listLegalDocuments();
      } catch (err) {
        console.error('Error loading legal documents:', err);
      }
      if (isAdmin) {
        try {
          acks = await listLegalAcknowledgments();
        } catch (err) {
          console.error('Error loading legal acknowledgments:', err);
        }
      }
    })();
  });

  function open(key: string) {
    viewerKey = key;
    viewerOpen = true;
  }

  const fmt = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : '');
</script>

<p class="legal-note">{$t('legal.settings.officialText')}</p>

<ul class="legal-list" data-testid="legal-doc-list">
  {#each docs as doc (doc.key)}
    <li class="legal-row">
      <div class="legal-row-main">
        <strong>{doc.title}</strong>
        <span class="legal-row-meta">{$t('legal.viewer.version', { version: doc.version })}</span>
        <span class="legal-badge" class:draft={doc.status === 'draft'} data-testid="legal-status">
          {doc.status === 'draft' ? $t('legal.draftBadge') : doc.status}
        </span>
      </div>
      <button class="btn btn-secondary" on:click={() => open(doc.key)}>
        {$t('legal.settings.read')}
      </button>
    </li>
  {/each}
</ul>

{#if isSuperAdmin && operatorDoc}
  <div class="legal-callout" data-testid="legal-own-ack">
    {#if operatorDoc.acknowledged}
      {$t('legal.settings.ownAcknowledged', { date: fmt(operatorDoc.acknowledged_at) })}
    {:else}
      {$t('legal.settings.ownNotAcknowledged')}
    {/if}
  </div>
{/if}

{#if isAdmin}
  <h4 class="legal-subtitle">{$t('legal.settings.ackTitle')}</h4>
  {#if acks.length}
    <div class="legal-table-wrap">
      <table class="legal-table" data-testid="legal-ack-table">
        <thead>
          <tr>
            <th>{$t('legal.settings.colUser')}</th>
            <th>{$t('legal.settings.colDocument')}</th>
            <th>{$t('legal.settings.colVersion')}</th>
            <th>{$t('legal.settings.colFingerprint')}</th>
            <th>{$t('legal.settings.colDate')}</th>
          </tr>
        </thead>
        <tbody>
          {#each acks as ack (`${ack.user_email}|${ack.key}|${ack.version}|${ack.accepted_at}`)}
            <tr>
              <td>{ack.user_email}</td>
              <td>{ack.key}</td>
              <td>{ack.version}</td>
              <td title={ack.sha256}>{ack.sha256.slice(0, 12)}</td>
              <td>{fmt(ack.accepted_at)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <p class="legal-note">{$t('legal.settings.noAcks')}</p>
  {/if}
{/if}

<LegalDocumentViewer isOpen={viewerOpen} docKey={viewerKey} onClose={() => (viewerOpen = false)} />

<style>
  .legal-note {
    margin: 0 0 12px;
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .legal-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 0 0 16px;
    padding: 0;
    list-style: none;
  }

  .legal-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 14px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--bg-secondary);
  }

  .legal-row-main {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 12px;
  }

  .legal-row-meta {
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
  }

  .legal-badge {
    padding: 2px 8px;
    border: 1px solid var(--border-color);
    border-radius: 999px;
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
  }

  .legal-badge.draft {
    border-color: var(--warning-color);
    background-color: rgba(var(--warning-color-rgb), 0.12);
  }

  .legal-callout {
    margin-block-end: 16px;
    padding: 10px 14px;
    border: 1px solid var(--border-color);
    border-inline-start-width: 4px;
    border-radius: 8px;
    background-color: var(--bg-secondary);
  }

  .legal-subtitle {
    margin: 0 0 8px;
  }

  .legal-table-wrap {
    overflow-x: auto;
  }

  .legal-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-sm);
  }

  .legal-table th,
  .legal-table td {
    padding: 6px 10px;
    border-block-end: 1px solid var(--border-color);
    text-align: start;
  }
</style>
