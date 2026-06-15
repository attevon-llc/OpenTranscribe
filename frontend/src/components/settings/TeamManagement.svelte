<!--
  TeamManagement.svelte — cloud-edition team/seat management (org admins only).

  Extends the existing groups/ UI (GroupsOverview → GroupMemberList,
  GroupCreateModal …) rather than building a fresh members/roles/invites surface.
  On top of it, this wrapper surfaces seat usage from the billing subscription
  (seats_limit) and warns when the org is at/over its seat allowance.

  Cloud edition only (gated upstream in SettingsModal by cap:organizations +
  audience=org_admin).
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import GroupsOverview from '$components/groups/GroupsOverview.svelte';
  import { BillingApi } from '$lib/api/billing';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import Badge from '$components/ui/Badge.svelte';

  let seatsLimit: number | null = null;
  let seatsUsed: number | null = null;
  let loading = true;

  onMount(() => {
    const controller = new AbortController();
    (async () => {
      await loadSeats();
    })();
    return () => controller.abort();
  });

  async function loadSeats() {
    loading = true;
    try {
      // Seat allowance comes from the plan; current seat consumption is the
      // distinct member count across the org's teams (usage breakdown is the
      // authoritative per-member list).
      const [subscription, usage] = await Promise.all([
        BillingApi.getSubscription(),
        BillingApi.getUsage().catch(() => null),
      ]);
      seatsLimit = subscription.seats_limit ?? null;
      seatsUsed = usage ? usage.members.length : null;
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('team.toast.loadFailed')));
    } finally {
      loading = false;
    }
  }

  // The seat cap is enforced server-side when adding members; this banner just
  // warns the admin proactively when the org is at/over its allowance.
  $: atSeatLimit =
    seatsLimit !== null && seatsUsed !== null && seatsUsed >= seatsLimit;
</script>

<div class="team-management">
  {#if !loading && seatsLimit !== null}
    <div class="seats-banner" class:over={atSeatLimit}>
      <div class="seats-info">
        <span class="seats-count">{seatsUsed ?? '—'} / {seatsLimit}</span>
        <span class="seats-label">{$t('team.seatsUsed')}</span>
      </div>
      {#if atSeatLimit}
        <Badge variant="warning">{$t('team.seatsFull')}</Badge>
      {:else}
        <Badge variant="success">{$t('team.seatsAvailable')}</Badge>
      {/if}
    </div>
    {#if atSeatLimit}
      <p class="seats-hint">{$t('team.seatsFullHint')}</p>
    {/if}
  {/if}

  <GroupsOverview />
</div>

<style>
  .team-management {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .seats-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
  }

  .seats-banner.over {
    border-color: var(--warning-color, #d97706);
  }

  .seats-info {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
  }

  .seats-count {
    font-size: 1.125rem;
    font-weight: 700;
    color: var(--text-color);
    font-variant-numeric: tabular-nums;
  }

  .seats-label {
    font-size: 0.8125rem;
    color: var(--text-secondary);
  }

  .seats-hint {
    margin: 0;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    line-height: 1.5;
  }
</style>
