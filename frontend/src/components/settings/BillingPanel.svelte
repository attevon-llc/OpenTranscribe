<!--
  BillingPanel.svelte — cloud-edition billing settings (org admins only).

  Shows the current plan / status / next billing date / overage from
  GET /api/billing/subscription, and exposes:
   - Stripe Checkout   (POST /api/billing/checkout-session)
   - Customer Portal    (POST /api/billing/portal-session)

  Uses the REAL route names (subscription / checkout-session / portal-session)
  and passes success_url / cancel_url / return_url (the backend open-redirect
  guards them). Gated by edition=cloud + cap:billing + audience=org_admin in
  SettingsModal; this component assumes it only renders in the cloud build.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { BillingApi, type BillingSubscription } from '$lib/api/billing';
  import { billingStore } from '$stores/cloudBilling';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { formatDate } from '$lib/utils/formatting';
  import Spinner from '$components/ui/Spinner.svelte';
  import Badge from '$components/ui/Badge.svelte';

  let subscription: BillingSubscription | null = null;
  let loading = true;
  let checkoutBusy = false;
  let portalBusy = false;

  onMount(() => {
    const controller = new AbortController();
    (async () => {
      await loadSubscription();
    })();
    return () => controller.abort();
  });

  async function loadSubscription() {
    loading = true;
    try {
      subscription = await BillingApi.getSubscription();
      billingStore.set({
        loaded: true,
        plan: subscription.plan ?? '',
        status: subscription.status ?? '',
        next_billing_date: subscription.next_billing_date ?? null,
      });
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('billing.toast.loadFailed')));
    } finally {
      loading = false;
    }
  }

  // Map subscription status to a Badge variant.
  function statusVariant(status: string): 'success' | 'warning' | 'error' | 'default' {
    switch ((status || '').toLowerCase()) {
      case 'active':
      case 'trialing':
        return 'success';
      case 'past_due':
      case 'incomplete':
        return 'warning';
      case 'canceled':
      case 'unpaid':
        return 'error';
      default:
        return 'default';
    }
  }

  async function startCheckout() {
    if (checkoutBusy) return;
    checkoutBusy = true;
    try {
      const { url } = await BillingApi.createCheckoutSession({
        success_url: `${window.location.origin}/?billing=success`,
        cancel_url: `${window.location.origin}/?billing=cancel`,
      });
      if (url) window.location.href = url;
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('billing.toast.checkoutFailed')));
      checkoutBusy = false;
    }
  }

  async function openPortal() {
    if (portalBusy) return;
    portalBusy = true;
    try {
      const { url } = await BillingApi.createPortalSession({
        return_url: `${window.location.origin}/?billing=portal`,
      });
      if (url) window.location.href = url;
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('billing.toast.portalFailed')));
      portalBusy = false;
    }
  }
</script>

<div class="billing-panel">
  {#if loading}
    <div class="loading-state">
      <Spinner size="medium" />
      <p>{$t('billing.loading')}</p>
    </div>
  {:else if subscription}
    <div class="billing-grid">
      <div class="billing-row">
        <span class="billing-label">{$t('billing.plan')}</span>
        <span class="billing-value plan-name">{subscription.plan || $t('billing.noPlan')}</span>
      </div>
      <div class="billing-row">
        <span class="billing-label">{$t('billing.status')}</span>
        <Badge variant={statusVariant(subscription.status)}>
          {subscription.status || $t('billing.statusUnknown')}
        </Badge>
      </div>
      <div class="billing-row">
        <span class="billing-label">{$t('billing.nextBilling')}</span>
        <span class="billing-value">
          {subscription.next_billing_date
            ? formatDate(subscription.next_billing_date)
            : $t('billing.notScheduled')}
        </span>
      </div>
      {#if subscription.included_hours !== null}
        <div class="billing-row">
          <span class="billing-label">{$t('billing.includedHours')}</span>
          <span class="billing-value">{subscription.included_hours}</span>
        </div>
      {/if}
      {#if subscription.seats_limit !== null}
        <div class="billing-row">
          <span class="billing-label">{$t('billing.seats')}</span>
          <span class="billing-value">{subscription.seats_limit}</span>
        </div>
      {/if}
      {#if subscription.overage_enabled && subscription.overage_rate !== null}
        <div class="billing-row">
          <span class="billing-label">{$t('billing.overageRate')}</span>
          <span class="billing-value">{$t('billing.overagePerHour', { rate: subscription.overage_rate })}</span>
        </div>
      {/if}
    </div>

    <div class="billing-actions">
      <button class="btn btn-primary" on:click={startCheckout} disabled={checkoutBusy}>
        {#if checkoutBusy}<Spinner size="small" color="white" />{/if}
        {$t('billing.changePlan')}
      </button>
      <button class="btn btn-secondary" on:click={openPortal} disabled={portalBusy}>
        {#if portalBusy}<Spinner size="small" />{/if}
        {$t('billing.manageBilling')}
      </button>
    </div>

    <p class="billing-note">{$t('billing.portalNote')}</p>
  {:else}
    <div class="error-state">
      <p>{$t('billing.loadFailed')}</p>
      <button class="btn btn-secondary" on:click={loadSubscription}>{$t('billing.retry')}</button>
    </div>
  {/if}
</div>

<style>
  .billing-panel {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }

  .loading-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.75rem;
    padding: 2rem 0;
    color: var(--text-secondary);
    font-size: 0.8125rem;
  }

  .loading-state p {
    margin: 0;
  }

  .billing-grid {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem;
    background: var(--surface-color);
  }

  .billing-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.375rem 0;
  }

  .billing-row + .billing-row {
    border-top: 1px solid var(--border-color);
  }

  .billing-label {
    font-size: 0.8125rem;
    color: var(--text-secondary);
  }

  .billing-value {
    font-size: 0.875rem;
    color: var(--text-color);
    font-weight: 500;
  }

  .plan-name {
    text-transform: capitalize;
  }

  .billing-actions {
    display: flex;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .billing-note {
    margin: 0;
    font-size: 0.75rem;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .error-state {
    text-align: center;
    padding: 1.5rem 0;
    color: var(--error-color, #dc2626);
    font-size: 0.8125rem;
  }

  .error-state p {
    margin: 0 0 0.75rem;
  }

  .btn {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    border: none;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-primary {
    background-color: var(--primary-color);
    color: white;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-primary:hover:not(:disabled) {
    background-color: var(--primary-hover);
    transform: translateY(-1px);
  }

  .btn-secondary {
    background-color: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
  }

  .btn-secondary:hover:not(:disabled) {
    background-color: var(--button-hover);
  }

  .btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
