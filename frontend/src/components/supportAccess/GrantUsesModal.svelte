<script lang="ts">
  import { SUPPORT_LEVEL_KEYS } from '$lib/i18n/keyMaps';
  import { locale, t } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import {
    SupportAccessApi,
    type GrantPerspective,
    type SupportGrant,
    type SupportGrantUse,
  } from '$lib/api/supportAccess';

  /**
   * The tenant's Access Transparency view: every request a support grant authorised.
   * "No recorded access" and "could not load" are different states and render differently.
   */
  export let isOpen = false;
  export let grant: SupportGrant | null = null;
  export let perspective: GrantPerspective = 'staff';
  export let onClose: () => void = () => {};

  const PAGE = 25;

  let uses: SupportGrantUse[] = [];
  let total = 0;
  let loading = false;
  let failed = false;
  let loadedFor: string | null = null;

  $: if (isOpen && grant && loadedFor !== grant.uuid) {
    loadedFor = grant.uuid;
    uses = [];
    total = 0;
    void load(0);
  }
  $: if (!isOpen) loadedFor = null;

  async function load(offset: number) {
    if (!grant) return;
    loading = true;
    failed = false;
    try {
      const page = await SupportAccessApi.listUses(perspective, grant.uuid, {
        limit: PAGE,
        offset,
      });
      uses = offset === 0 ? page.items : [...uses, ...page.items];
      total = page.total;
    } catch {
      failed = true;
    } finally {
      loading = false;
    }
  }

  function when(iso: string): string {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString($locale);
  }
</script>

<BaseModal {isOpen} {onClose} title={$t('supportAccess.uses.title')} maxWidth="760px">
  {#if failed}
    <p class="state error" role="alert">{$t('supportAccess.uses.loadFailed')}</p>
  {:else if loading && uses.length === 0}
    <div class="state"><Spinner /></div>
  {:else if uses.length === 0}
    <p class="state">{$t('supportAccess.uses.empty')}</p>
  {:else}
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">{$t('supportAccess.uses.col.time')}</th>
            <th scope="col">{$t('supportAccess.uses.col.method')}</th>
            <th scope="col">{$t('supportAccess.uses.col.route')}</th>
            <th scope="col">{$t('supportAccess.uses.col.resource')}</th>
            <th scope="col">{$t('supportAccess.uses.col.need')}</th>
          </tr>
        </thead>
        <tbody>
          {#each uses as use, i (i)}
            <tr>
              <td class="nowrap"><bdi>{when(use.occurred_at)}</bdi></td>
              <td><bdi>{use.method}</bdi></td>
              <td><code><bdi>{use.route}</bdi></code></td>
              <td>
                {#if use.resource_type || use.resource_uuid}
                  {use.resource_type ?? ''}
                  {#if use.resource_uuid}<code class="uuid"><bdi>{use.resource_uuid}</bdi></code>{/if}
                {/if}
              </td>
              <td>{use.need ? $t(SUPPORT_LEVEL_KEYS[use.need]) : ''}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if uses.length < total}
      <button
        type="button"
        class="btn btn-secondary more"
        disabled={loading}
        on:click={() => load(uses.length)}
      >
        {$t('supportAccess.loadMore')}
      </button>
    {/if}
  {/if}
</BaseModal>

<style>
  .state {
    margin: 1rem 0;
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.875rem;
  }
  .state.error {
    color: var(--error-color);
  }
  .table-wrap {
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }
  th {
    text-align: start;
    padding: 0.4rem 0.6rem;
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-color);
    white-space: nowrap;
  }
  td {
    padding: 0.5rem 0.6rem;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-color);
    vertical-align: top;
  }
  .nowrap {
    white-space: nowrap;
  }
  code {
    font-size: 0.75rem;
    overflow-wrap: anywhere;
  }
  .uuid {
    display: block;
    color: var(--text-secondary);
  }
  .more {
    display: block;
    margin: 1rem auto 0 auto;
  }
</style>
