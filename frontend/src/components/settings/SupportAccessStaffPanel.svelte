<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { user as userStore } from '$stores/auth';
  import { toastStore } from '$stores/toast';
  import { supportSession } from '$stores/supportSession';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { SupportAccessApi, type SupportGrant } from '$lib/api/supportAccess';
  import { SUPPORT_ACCESS_EVENT } from '$lib/supportAccess/events';
  import { grantTargetLabel } from '$lib/supportAccess/format';
  import Tabs, { type TabItem } from '$components/ui/Tabs.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import GrantList from '$components/supportAccess/GrantList.svelte';
  import RequestAccessModal from '$components/supportAccess/RequestAccessModal.svelte';
  import BreakGlassModal from '$components/supportAccess/BreakGlassModal.svelte';
  import GrantDecisionNoteModal from '$components/supportAccess/GrantDecisionNoteModal.svelte';
  import GrantUsesModal from '$components/supportAccess/GrantUsesModal.svelte';
  import OpenFileByUuid from '$components/supportAccess/OpenFileByUuid.svelte';

  /**
   * Platform support staff: request access to a tenant, start and end a session, revoke,
   * and (super_admin) break glass. Coordinator only; rendering lives in the children.
   */
  const PAGE = 25;
  const POLL_MS = 30_000;

  $: isSuperAdmin = $userStore?.role === 'super_admin';

  let scope: 'mine' | 'all' = 'mine';
  let grants: SupportGrant[] = [];
  let total = 0;
  let loading = false;
  let loaded = false;
  let loadFailed = false;
  let busyUuid: string | null = null;

  let requestOpen = false;
  let breakGlassOpen = false;
  let revoking: SupportGrant | null = null;
  let logGrant: SupportGrant | null = null;
  let logOpen = false;

  $: tabs = [
    { id: 'mine', label: $t('supportAccess.tabs.mine') },
    { id: 'all', label: $t('supportAccess.tabs.all') },
  ] as TabItem[];
  $: openPersonal = grants.filter((g) => g.status === 'active' && g.target_kind === 'personal');

  /** Reload the first page(s). Quiet reloads (polling) keep the list on a failed refresh. */
  async function load(quiet = false, append = false) {
    if (!quiet) loading = true;
    try {
      const offset = append ? grants.length : 0;
      const limit = append ? PAGE : Math.min(100, Math.max(PAGE, grants.length));
      const page = await SupportAccessApi.listMyGrants({ scope, limit, offset });
      grants = append ? [...grants, ...page.items] : page.items;
      total = page.total;
      loadFailed = false;
    } catch (err: unknown) {
      if (!quiet) {
        grants = [];
        loadFailed = true;
        toastStore.error(getErrorMessage(err, $t('supportAccess.loadFailed')));
      }
    } finally {
      loading = false;
      loaded = true;
    }
  }

  function changeScope(event: CustomEvent<string>) {
    scope = event.detail === 'all' ? 'all' : 'mine';
    grants = [];
    void load();
  }

  async function start(grant: SupportGrant) {
    busyUuid = grant.uuid;
    try {
      if (await supportSession.activate(grant.uuid)) settingsModalStore.close();
    } finally {
      busyUuid = null;
    }
  }

  async function revoke(event: CustomEvent<{ note?: string }>) {
    const grant = revoking;
    if (!grant) return;
    busyUuid = grant.uuid;
    try {
      await SupportAccessApi.revokeGrant(grant.uuid, event.detail);
      toastStore.success($t('supportAccess.revoke.revoked'));
      if ($supportSession.grantUuid === grant.uuid) await supportSession.end('revoked');
      revoking = null;
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('supportAccess.revoke.failed')));
    } finally {
      busyUuid = null;
      await load(true);
    }
  }

  function onCreated(event: CustomEvent<SupportGrant>) {
    grants = [event.detail, ...grants];
    total += 1;
  }

  async function startFromBreakGlass(event: CustomEvent<SupportGrant>) {
    if (await supportSession.activate(event.detail.uuid)) settingsModalStore.close();
  }

  const onPush = () => void load(true);
  let timer: ReturnType<typeof setInterval> | undefined;

  onMount(() => {
    void load();
    timer = setInterval(() => void load(true), POLL_MS);
    window.addEventListener(SUPPORT_ACCESS_EVENT, onPush);
  });
  onDestroy(() => {
    if (timer) clearInterval(timer);
    if (typeof window !== 'undefined') window.removeEventListener(SUPPORT_ACCESS_EVENT, onPush);
  });
</script>

<div class="panel">
  <header class="head">
    <p class="description">{$t('settings.supportAccess.description')}</p>
    <div class="actions">
      <button type="button" class="btn btn-primary" on:click={() => (requestOpen = true)}>
        {$t('supportAccess.action.request')}
      </button>
      <!-- A disabled button shows no tooltip in several browsers, so the title lives on the wrapper. -->
      <span class="bg-wrap" title={isSuperAdmin ? undefined : $t('supportAccess.breakGlass.superAdminOnly')}>
        <button
          type="button"
          class="btn btn-danger sa-danger"
          disabled={!isSuperAdmin}
          aria-describedby={isSuperAdmin ? undefined : 'bg-locked-reason'}
          on:click={() => (breakGlassOpen = true)}
        >
          {#if !isSuperAdmin}
            <svg class="lock" aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
          {/if}
          {$t('supportAccess.action.breakGlass')}
        </button>
        {#if !isSuperAdmin}
          <span id="bg-locked-reason" class="sr-only">{$t('supportAccess.breakGlass.superAdminOnly')}</span>
        {/if}
      </span>
    </div>
  </header>

  {#if isSuperAdmin}
    <Tabs {tabs} activeId={scope} on:change={changeScope} />
  {/if}

  <div
    role={isSuperAdmin ? 'tabpanel' : undefined}
    id={isSuperAdmin ? `tabpanel-${scope}` : undefined}
    aria-labelledby={isSuperAdmin ? `tab-${scope}` : undefined}
  >
  {#if loading && !loaded}
    <div class="state"><Spinner /></div>
  {:else if loadFailed}
    <p class="state error" role="alert">{$t('supportAccess.loadFailed')}</p>
  {:else if grants.length === 0}
    <p class="state">{$t('supportAccess.empty.staff')}</p>
  {:else}
    <GrantList
      {grants}
      perspective="staff"
      caption={$t('settings.supportAccess.title')}
      activeGrantUuid={$supportSession.grantUuid}
      {busyUuid}
      on:start={(e) => start(e.detail)}
      on:revoke={(e) => (revoking = e.detail)}
      on:viewLog={(e) => {
        logGrant = e.detail;
        logOpen = true;
      }}
    />
    {#if grants.length < total}
      <button type="button" class="btn btn-secondary more" disabled={loading} on:click={() => load(false, true)}>
        {$t('supportAccess.loadMore')}
      </button>
    {/if}
    {#if openPersonal.length > 0}
      <OpenFileByUuid />
    {/if}
  {/if}
  </div>
</div>

<RequestAccessModal isOpen={requestOpen} onClose={() => (requestOpen = false)} on:created={onCreated} />
<BreakGlassModal
  isOpen={breakGlassOpen}
  onClose={() => (breakGlassOpen = false)}
  on:created={onCreated}
  on:start={startFromBreakGlass}
/>
<GrantDecisionNoteModal
  isOpen={revoking !== null}
  mode="revoke"
  name={revoking ? grantTargetLabel(revoking, $t) : ''}
  busy={busyUuid !== null}
  onClose={() => (revoking = null)}
  on:submit={revoke}
/>
<GrantUsesModal
  isOpen={logOpen}
  grant={logGrant}
  perspective="staff"
  onClose={() => (logOpen = false)}
/>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
  }
  .description {
    flex: 1 1 18rem;
    margin: 0;
    font-size: 0.875rem;
    line-height: 1.5;
    color: var(--text-secondary);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  .bg-wrap {
    display: inline-flex;
  }
  .lock {
    margin-inline-end: 0.35rem;
    vertical-align: -2px;
  }
  .state {
    margin: 1.5rem 0;
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.875rem;
  }
  .state.error {
    color: var(--error-color);
  }
  .more {
    align-self: center;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
  @media (max-width: 768px) {
    .actions .btn {
      min-height: 44px;
    }
  }
</style>
