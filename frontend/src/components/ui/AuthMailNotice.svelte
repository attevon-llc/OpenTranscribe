<!--
  AuthMailNotice.svelte -- says so, in the place the admin is looking, when
  authentication email cannot be delivered (issue #1240).

  Password resets, invitations and verification links all depend on one thing: a
  transport (a designated email configuration, else the SMTP_* environment). The
  backend refuses to pretend such a message was sent, but until now the only
  place that said so was one tab, for super admins, after they went looking. This
  notice is rendered where the consequence lands: the invite form and the
  Auth Email tab.

  It shows ONLY on a definite `deliverable === false`. Anything else renders
  nothing: `null` (the readiness could not be read: 403 for a plain admin, 404 on an
  edition without the auth.config_ui capability, or a network error) is
  "unknown", and an advisory notice that treated unknown as broken would show a
  false alarm to exactly the people who cannot fix it. It also never says
  "working" for the other state: `deliverable` means configured, and a wrong host
  still reads true; only a real send proves delivery.

  Copy is a per-context MAP, not a ternary: a ternary renders the `else` branch's
  text for any context it was not written for, with every test still green (the
  lesson recorded in RetrievalQualityNotice.svelte). `AuthMailNotice.test.ts`
  enumerates the contexts.

  `readiness` omitted -> the component fetches it itself (invite form).
  `readiness` passed (including `null`) -> it renders that, no fetch (the tab, which
  already holds the designation it is editing and must update when it saves).
-->
<script context="module" lang="ts">
  export type AuthMailNoticeContext = 'invite' | 'tab';
</script>

<script lang="ts">
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { AuthConfigApi, type AuthMailDesignation } from '$lib/api/authConfig';

  /** Where the notice is rendered -- selects the copy. */
  export let context: AuthMailNoticeContext = 'tab';
  /** The designation to describe; leave `undefined` to have the notice fetch it. */
  export let readiness: AuthMailDesignation | null | undefined = undefined;

  /** The page that explains why a relay or the business's own provider is needed. */
  const DOCS_URL = 'https://docs.opentranscribe.app/docs/authentication/email-delivery';

  /** Body copy for the contexts that do not depend on the designation's state. */
  const BODY_KEY_BY_CONTEXT: Record<AuthMailNoticeContext, string | null> = {
    invite: 'authMailNotice.inviteBody',
    // The tab's body depends on WHY nothing is deliverable; see TAB_BODY_KEY_BY_STATUS.
    tab: null,
  };

  /** Why nothing is deliverable, on the tab where the admin can act on it. */
  const TAB_BODY_KEY_BY_STATUS: Record<AuthMailDesignation['status'], string> = {
    not_designated: 'authMailNotice.tabNotDesignated',
    missing: 'authMailNotice.tabMissing',
    disabled: 'authMailNotice.tabDisabled',
    // Unreachable while `deliverable` is false (an active designation delivers);
    // present so the map is total over the union and a new status fails type-check.
    active: 'authMailNotice.tabNotDesignated',
  };

  onMount(async () => {
    if (readiness === undefined) readiness = await AuthConfigApi.getAuthMailReadiness();
  });

  $: show = readiness != null && readiness.deliverable === false;
  $: bodyKey =
    BODY_KEY_BY_CONTEXT[context] ??
    (readiness ? TAB_BODY_KEY_BY_STATUS[readiness.status] : 'authMailNotice.tabNotDesignated');
  /** With verification required, no mail also means no new local sign-ins. */
  $: escalated = show && readiness?.verification_required === true;
</script>

{#if show}
  <div
    class="auth-mail-notice"
    class:escalated
    role="alert"
    data-testid="auth-mail-notice"
    data-context={context}
  >
    <strong class="notice-heading">{$t('authMailNotice.heading')}</strong>
    <p class="notice-body">
      {$t(bodyKey, { name: readiness?.config_name ?? '' })}
    </p>
    {#if context === 'tab'}
      <p class="notice-body">{$t('authMailNotice.affects')}</p>
    {/if}
    {#if escalated}
      <p class="notice-body notice-escalation" data-testid="auth-mail-notice-escalation">
        {$t('authMailNotice.verificationRequired')}
      </p>
    {/if}
    <a class="notice-link" href={DOCS_URL} target="_blank" rel="noopener noreferrer">
      {$t('authMailNotice.docsLink')}
    </a>
  </div>
{/if}

<style>
  /* Warning tokens, escalated to the error tokens when sign-in itself is blocked.
     Both are theme tokens, so light and dark need no second rule set. */
  .auth-mail-notice {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-block: 4px 8px;
    padding: 8px 10px;
    border: 1px solid var(--warning-border);
    border-inline-start-width: 4px;
    border-radius: 6px;
    background: var(--warning-bg);
    color: var(--text-color);
    font-size: 0.8rem;
    line-height: 1.45;
    text-align: start;
  }
  .auth-mail-notice.escalated {
    border-color: var(--error-border);
    background: var(--error-background);
  }
  .notice-heading {
    font-size: 0.85rem;
  }
  .notice-body {
    margin: 0;
  }
  .notice-link {
    align-self: flex-start;
    color: var(--primary-on-surface);
    text-decoration: underline;
    text-underline-offset: 2px;
  }
  .notice-link:hover {
    color: var(--primary-hover);
  }
  .notice-link:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
    border-radius: 2px;
  }
</style>
