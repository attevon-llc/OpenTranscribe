<script lang="ts">
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { GroupsApi } from '$lib/api/groups';
  import type { Group } from '$lib/types/groups';
  import { GroupMappingsApi, type GroupMapping, type GroupMappingSource } from '$lib/api/groupMappings';
  import EmptyState from '../ui/EmptyState.svelte';
  import Badge from '../ui/Badge.svelte';
  import ConfirmationModal from '../ConfirmationModal.svelte';
  import GroupMappingForm from './GroupMappingForm.svelte';
  import GroupMappingTester from './GroupMappingTester.svelte';

  /**
   * IdP group mappings — the one screen for `/api/admin/group-mappings`.
   *
   * A mapping turns a directory claim (`CN=Legal-Team,OU=Groups,…` from LDAP, a
   * role/group value from OIDC) into in-app membership and, optionally, a role.
   * The mapping set is per-source, so the source toggle is the primary filter
   * rather than a column: LDAP DNs and OIDC role names never mix in one list.
   */
  let source: GroupMappingSource = 'ldap';
  let mappings: GroupMapping[] = [];
  let groups: Group[] = [];
  let loading = false;

  let showForm = false;
  let editing: GroupMapping | null = null;

  let showDeleteConfirm = false;
  let pendingDelete: GroupMapping | null = null;

  onMount(async () => {
    // The picker options are source-independent, so they are fetched once.
    groups = await GroupsApi.fetchGroups().catch(() => []);
    await loadMappings();
  });

  async function loadMappings() {
    loading = true;
    try {
      mappings = await GroupMappingsApi.list(source);
    } catch (err: unknown) {
      mappings = [];
      toastStore.error(getErrorMessage(err, $t('settings.groupMappings.loadFailed')));
    } finally {
      loading = false;
    }
  }

  function selectSource(next: GroupMappingSource) {
    if (next === source) return;
    source = next;
    void loadMappings();
  }

  function openCreate() {
    editing = null;
    showForm = true;
  }

  function openEdit(mapping: GroupMapping) {
    editing = mapping;
    showForm = true;
  }

  function askDelete(mapping: GroupMapping) {
    pendingDelete = mapping;
    showDeleteConfirm = true;
  }

  async function confirmDelete() {
    const target = pendingDelete;
    pendingDelete = null;
    if (!target) return;
    try {
      await GroupMappingsApi.remove(target.uuid);
      toastStore.success($t('settings.groupMappings.toast.deleted'));
      await loadMappings();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.groupMappings.toast.deleteFailed')));
    }
  }
</script>

<div class="settings-panel">
  <div class="info-box">
    <strong>{$t('settings.groupMappings.infoTitle')}</strong>
    <p>{$t('settings.groupMappings.infoDescription')}</p>
  </div>

  <div class="source-row">
    <div class="source-toggle" role="group" aria-label={$t('settings.groupMappings.sourceLabel')}>
      <button
        type="button"
        class="source-button"
        class:active={source === 'ldap'}
        aria-pressed={source === 'ldap'}
        on:click={() => selectSource('ldap')}
      >
        {$t('settings.groupMappings.sourceLdap')}
      </button>
      <button
        type="button"
        class="source-button"
        class:active={source === 'oidc'}
        aria-pressed={source === 'oidc'}
        on:click={() => selectSource('oidc')}
      >
        {$t('settings.groupMappings.sourceOidc')}
      </button>
    </div>

    <button type="button" class="btn btn-primary" on:click={openCreate}>
      {$t('settings.groupMappings.addMapping')}
    </button>
  </div>

  {#if loading}
    <div class="loading">{$t('common.loading')}</div>
  {:else if mappings.length === 0}
    <EmptyState
      icon="🔗"
      title={$t('settings.groupMappings.emptyTitle')}
      description={source === 'ldap'
        ? $t('settings.groupMappings.emptyDescriptionLdap')
        : $t('settings.groupMappings.emptyDescriptionOidc')}
      padding="32px 16px"
    />
  {:else}
    <div class="table-wrap">
      <table class="mapping-table">
        <thead>
          <tr>
            <th>{$t('settings.groupMappings.claimValue')}</th>
            <th>{$t('settings.groupMappings.targetGroup')}</th>
            <th>{$t('settings.groupMappings.grantsRole')}</th>
            <th class="numeric">{$t('settings.groupMappings.members')}</th>
            <th class="actions-col">{$t('common.actions')}</th>
          </tr>
        </thead>
        <tbody>
          {#each mappings as mapping (mapping.uuid)}
            <tr>
              <td>
                <span class="claim">{mapping.claim_value}</span>
                {#if mapping.description}
                  <span class="description">{mapping.description}</span>
                {/if}
              </td>
              <td>
                {#if mapping.group_name}
                  <Badge variant="success">{mapping.group_name}</Badge>
                {:else}
                  <span class="muted">{$t('settings.groupMappings.noGroup')}</span>
                {/if}
              </td>
              <td>
                {#if mapping.grants_role}
                  <Badge variant={mapping.grants_role === 'admin' ? 'warning' : 'info'}>
                    {$t(`settings.groupMappings.role.${mapping.grants_role}`)}
                  </Badge>
                {:else}
                  <span class="muted">{$t('settings.groupMappings.noRole')}</span>
                {/if}
              </td>
              <td class="numeric">{mapping.member_count}</td>
              <td class="actions-col">
                <button type="button" class="btn btn-secondary btn-row" on:click={() => openEdit(mapping)}>
                  {$t('common.edit')}
                </button>
                <button type="button" class="btn btn-danger btn-row" on:click={() => askDelete(mapping)}>
                  {$t('common.delete')}
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <GroupMappingTester {source} />
</div>

<GroupMappingForm
  bind:isOpen={showForm}
  {source}
  mapping={editing}
  {groups}
  on:saved={loadMappings}
/>

<ConfirmationModal
  bind:isOpen={showDeleteConfirm}
  title={$t('settings.groupMappings.deleteTitle')}
  message={$t('settings.groupMappings.deleteMessage', { claim: pendingDelete?.claim_value ?? '' })}
  confirmText={$t('common.delete')}
  on:confirm={confirmDelete}
  on:cancel={() => (pendingDelete = null)}
/>

<style>
  .settings-panel {
    max-width: 900px;
  }

  .info-box {
    background: var(--color-info-bg);
    border: 1px solid var(--color-info-border);
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 1.5rem;
  }

  .info-box strong {
    color: var(--color-text);
  }

  .info-box p {
    margin: 0.5rem 0 0 0;
    font-size: 0.875rem;
    color: var(--color-text-secondary);
    line-height: 1.5;
  }

  .source-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1rem;
    flex-wrap: wrap;
  }

  .source-toggle {
    display: inline-flex;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    overflow: hidden;
  }

  .source-button {
    padding: 0.45rem 1rem;
    background: var(--color-bg);
    border: none;
    border-radius: 0;
    box-shadow: none;
    color: var(--color-text-secondary);
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
  }

  .source-button:hover {
    background: var(--color-bg-secondary);
    transform: none;
    box-shadow: none;
  }

  .source-button.active {
    background: var(--color-primary);
    color: #fff;
  }

  .loading {
    text-align: center;
    padding: 2rem;
    color: var(--color-text-secondary);
  }

  .table-wrap {
    overflow-x: auto;
  }

  .mapping-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .mapping-table th {
    text-align: left;
    padding: 0.5rem 0.6rem;
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--color-text-secondary);
    border-bottom: 1px solid var(--color-border);
    white-space: nowrap;
  }

  .mapping-table td {
    padding: 0.6rem;
    border-bottom: 1px solid var(--color-border);
    color: var(--color-text);
    vertical-align: top;
  }

  .mapping-table .numeric {
    text-align: right;
  }

  .claim {
    display: block;
    font-family: var(--font-mono, ui-monospace, monospace);
    word-break: break-all;
  }

  .description {
    display: block;
    margin-top: 0.2rem;
    font-size: 0.75rem;
    color: var(--color-text-tertiary);
  }

  .muted {
    color: var(--color-text-tertiary);
  }

  .actions-col {
    text-align: right;
    white-space: nowrap;
  }

  .btn-row {
    padding: 0.3rem 0.65rem;
    font-size: 0.75rem;
    margin-left: 0.35rem;
  }

  @media (max-width: 768px) {
    .source-row {
      flex-direction: column;
      align-items: stretch;
    }

    .source-button {
      flex: 1;
      min-height: 44px;
    }
  }
</style>
