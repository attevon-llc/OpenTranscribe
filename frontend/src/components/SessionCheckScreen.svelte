<!--
  What the visitor sees while the first `GET /auth/session` is in flight (or the server is
  unreachable), instead of a blank page.

  `shell` is true on a protected route. It paints only what is certainly there and certainly
  the same for everyone: a bar with the real navbar's height, surface and border and the real
  brand banner at the real navbar's left offset, over a neutral content area with a centred
  loader. Nothing else is drawn. Nav links, the notification bell and the user menu depend on
  role, capabilities and the account, none of which is known yet, so placeholders for them (an
  earlier version drew a card grid and an avatar) show a page that then re-arranges itself and
  imply components that may never appear. It carries no account data and none of the real
  app's selectors (`.navbar`, `.gallery-*`, `.search-page`), so it cannot leak authenticated
  UI to a logged-out visitor and cannot satisfy an e2e readiness wait. On a public route
  (/login) only the brand mark shows, since the form itself is the next paint.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import { sessionProbe, retrySessionProbeNow } from '$lib/auth/sessionProbe';
  import logoBanner from '../assets/logo-banner.png';

  export let shell = false;
</script>

{#if shell}
  <div class="check-shell" role="status" aria-busy="true" aria-label={$t('common.loading')}>
    <div class="check-bar">
      <div class="check-bar-inner">
        <img src={logoBanner} alt="" class="check-banner" />
      </div>
    </div>
    <div class="check-body">
      <div class="bar"><div class="bar-fill"></div></div>
    </div>
  </div>
{:else}
  <div class="check-brand">
    <img src="/icons/icon-192x192.png" alt="OpenTranscribe" class="logo" width="64" height="64" />
    <div class="bar"><div class="bar-fill"></div></div>
  </div>
{/if}

{#if $sessionProbe.unreachable}
  <div class="notice" role="alert">
    <strong>{$t('auth.sessionProbe.title')}</strong>
    <span>{$t('auth.sessionProbe.body')}</span>
    <button type="button" on:click={retrySessionProbeNow}>{$t('auth.sessionProbe.retry')}</button>
  </div>
{/if}

<style>
  .check-shell {
    min-height: 100vh;
    min-height: 100dvh;
    background: var(--bg-color, var(--bg-primary));
  }

  /* Same box as Navbar's `.navbar` / `.navbar-container`: height, surface, padding, max-width. */
  .check-bar {
    height: calc(60px + env(safe-area-inset-top, 0px));
    padding-top: env(safe-area-inset-top, 0px);
    background: var(--navbar-bg);
    border-bottom: 1px solid var(--navbar-border);
    box-shadow: var(--navbar-shadow);
  }

  .check-bar-inner {
    display: flex;
    align-items: center;
    height: 100%;
    max-width: 1600px;
    margin: 0 auto;
    padding: 0.75rem 1.5rem;
  }

  /* NavbarBrand: a 36px banner inside a 0.25rem-padded button, stepping down with the viewport. */
  .check-banner {
    box-sizing: content-box;
    height: 36px;
    width: auto;
    padding: 0.25rem;
    object-fit: contain;
    border-radius: 6px;
  }

  .check-body {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: calc(100vh - 60px);
    min-height: calc(100dvh - 60px);
  }

  /* Navbar.svelte's own breakpoints for `.navbar-container` padding and the banner height. */
  @media (max-width: 1560px) {
    .check-bar-inner {
      padding: 0.5rem 1rem;
    }
  }

  @media (max-width: 1024px) {
    .check-banner {
      height: 30px;
    }
  }

  @media (max-width: 768px) {
    .check-banner {
      height: 28px;
    }

    .check-bar-inner {
      padding: 0.5rem;
    }
  }

  @media (max-width: 480px) {
    .check-banner {
      height: 24px;
    }

    .check-bar-inner {
      padding: 0.5rem 0.375rem;
    }
  }

  .check-brand {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 1.5rem;
    min-height: 100vh;
    min-height: 100dvh;
    background-color: var(--bg-primary, #f8fafc);
  }

  .logo {
    border-radius: 16px;
    animation: check-pulse 1.8s ease-in-out infinite;
  }

  .bar {
    width: 120px;
    height: 3px;
    overflow: hidden;
    border-radius: 3px;
    background: var(--border-color, #e2e8f0);
  }

  .bar-fill {
    width: 40%;
    height: 100%;
    border-radius: 3px;
    background: var(--primary-color);
    animation: check-slide 1.2s ease-in-out infinite;
  }

  @keyframes check-pulse {
    50% {
      opacity: 0.6;
    }
  }

  @keyframes check-slide {
    0% {
      transform: translateX(-100%);
    }
    100% {
      transform: translateX(350%);
    }
  }

  :global([dir='rtl']) .bar-fill {
    animation-direction: reverse;
  }

  .notice {
    position: fixed;
    inset-inline: 1rem;
    bottom: 1.5rem;
    z-index: var(--z-critical, 10000);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 1rem;
    max-width: 32rem;
    margin-inline: auto;
    padding: 0.875rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 12px;
    background: var(--card-background);
    color: var(--text-color);
    box-shadow: var(--card-shadow);
  }

  .notice span {
    flex: 1 1 12rem;
    color: var(--text-secondary);
  }

  .notice button {
    padding: 0.4rem 0.9rem;
    border: 1px solid var(--primary-color);
    border-radius: 8px;
    background: transparent;
    color: var(--primary-color);
    font: inherit;
    cursor: pointer;
  }

  .notice button:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  @media (prefers-reduced-motion: reduce) {
    .logo {
      animation: none;
    }
    .bar-fill {
      animation: none;
      width: 100%;
    }
  }
</style>
