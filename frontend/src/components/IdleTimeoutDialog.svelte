<!--
  IdleTimeoutDialog.svelte — idle / absolute session timeout UI (issue #1106).

  Mounted by the root layout only for sessions owned by an external identity
  provider. Starts the guard on mount and stops it on destroy; renders the warning
  ("Stay signed in") and, once expired, an opaque lock screen that conceals the page
  (AC-11) until sign-out completes. Sign-out waits for in-flight uploads; the lock
  does not.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { hasActiveUploads } from '$stores/uploads';
  import { focusTrap } from '$lib/actions/focusTrap';
  import {
    sessionLock,
    loadSessionTimeoutConfig,
    startSessionTimeouts,
    stopSessionTimeouts,
    staySignedIn,
    endExpiredSession,
  } from '$lib/auth/sessionTimeouts';

  let destroyed = false;
  let now = Date.now();
  let clock: ReturnType<typeof setInterval> | null = null;
  let signingOut = false;

  onMount(() => {
    void (async () => {
      const config = await loadSessionTimeoutConfig();
      if (destroyed || !config) return;
      startSessionTimeouts(config, { hasActiveUploads, endSession: endExpiredSession });
    })();
  });

  onDestroy(() => {
    destroyed = true;
    if (clock !== null) clearInterval(clock);
    stopSessionTimeouts();
  });

  $: warning = $sessionLock.phase === 'warning' ? $sessionLock : null;
  $: locked = $sessionLock.phase === 'locked' ? $sessionLock : null;

  // A 1 s countdown only while the warning is up.
  $: if (warning && clock === null) {
    now = Date.now();
    clock = setInterval(() => (now = Date.now()), 1000);
  } else if (!warning && clock !== null) {
    clearInterval(clock);
    clock = null;
  }

  function formatRemaining(ms: number): string {
    const total = Math.max(0, Math.ceil(ms / 1000));
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return `${minutes}:${String(seconds).padStart(2, '0')}`;
  }

  $: remaining = warning ? formatRemaining(warning.deadline - now) : '';

  async function signOutNow(reason: 'idle' | 'absolute'): Promise<void> {
    if (signingOut) return;
    signingOut = true;
    try {
      await endExpiredSession(reason);
    } finally {
      signingOut = false;
    }
  }
</script>

{#if locked}
  <div class="session-lock" data-testid="session-lock">
    <div
      class="session-dialog"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="session-lock-title"
      aria-describedby="session-lock-message"
      use:focusTrap={{ enabled: true }}
    >
      <h2 id="session-lock-title">{$t('auth.sessionTimeout.lockedTitle')}</h2>
      <p id="session-lock-message">
        {locked.reason === 'absolute'
          ? $t('auth.sessionTimeout.lockedAbsolute')
          : $t('auth.sessionTimeout.lockedIdle')}
      </p>
      {#if locked.waitingForUploads}
        <p class="session-note" role="status">{$t('auth.sessionTimeout.waitingForUploads')}</p>
        <div class="session-actions">
          <button
            type="button"
            class="btn btn-secondary"
            disabled={signingOut}
            on:click={() => signOutNow(locked?.reason ?? 'idle')}
          >
            {$t('auth.sessionTimeout.signOutNow')}
          </button>
        </div>
      {:else}
        <p class="session-note" role="status">{$t('auth.sessionTimeout.signingOut')}</p>
      {/if}
    </div>
  </div>
{:else if warning}
  <div class="session-backdrop">
    <div
      class="session-dialog"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="session-warning-title"
      aria-describedby="session-warning-message"
      data-testid="session-warning"
      use:focusTrap={{ enabled: true }}
    >
      {#if warning.reason === 'absolute'}
        <h2 id="session-warning-title">{$t('auth.sessionTimeout.absoluteWarningTitle')}</h2>
        <p id="session-warning-message">
          {$t('auth.sessionTimeout.absoluteWarning', { time: remaining })}
        </p>
        <div class="session-actions">
          <button
            type="button"
            class="btn btn-primary"
            disabled={signingOut}
            on:click={() => signOutNow('absolute')}
          >
            {$t('auth.sessionTimeout.signInAgain')}
          </button>
        </div>
      {:else}
        <h2 id="session-warning-title">{$t('auth.sessionTimeout.warningTitle')}</h2>
        <p id="session-warning-message">
          {$t('auth.sessionTimeout.idleWarning', { time: remaining })}
        </p>
        <!-- "Stay signed in" first: the focus trap focuses the first button, and it
             is the safe default for a keyboard user. -->
        <div class="session-actions">
          <button type="button" class="btn btn-primary" on:click={staySignedIn}>
            {$t('auth.sessionTimeout.staySignedIn')}
          </button>
          <button
            type="button"
            class="btn btn-secondary"
            disabled={signingOut}
            on:click={() => signOutNow('idle')}
          >
            {$t('auth.sessionTimeout.signOutNow')}
          </button>
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .session-backdrop,
  .session-lock {
    position: fixed;
    inset: 0;
    z-index: var(--z-critical);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1rem;
  }

  .session-backdrop {
    background: rgba(0, 0, 0, 0.5);
  }

  /* Opaque on purpose: a locked session must conceal what was on screen (AC-11). */
  .session-lock {
    background: var(--background-color);
  }

  .session-dialog {
    width: 100%;
    max-width: 440px;
    background: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1.5rem;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.25);
  }

  .session-dialog h2 {
    margin: 0 0 0.75rem;
    font-size: 1.25rem;
  }

  .session-dialog p {
    margin: 0 0 0.75rem;
    line-height: 1.5;
  }

  .session-note {
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  .session-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
    margin-top: 1rem;
  }
</style>
