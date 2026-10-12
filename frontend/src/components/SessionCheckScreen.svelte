<!--
  What the visitor sees while the first `GET /auth/session` is in flight (or the server is
  unreachable), instead of a blank page.

  `shell` is true on a protected route: an empty app frame (bar + toolbar + card grid) paints at
  once so the app feels instant. It carries NO account data and none of the real app's selectors
  (`.navbar`, `.gallery-*`, `.search-page`), so it cannot leak authenticated UI to a logged-out
  visitor and cannot satisfy an e2e readiness wait. On a public route (/login) only the brand
  mark shows, since the form itself is the next paint.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import { sessionProbe, retrySessionProbeNow } from '$lib/auth/sessionProbe';

  export let shell = false;
</script>

{#if shell}
  <div class="check-shell" role="status" aria-busy="true" aria-label={$t('common.loading')}>
    <div class="check-bar">
      <img src="/icons/icon-192x192.png" alt="" width="32" height="32" />
      <span class="block" style="width: 7rem"></span>
      <span class="block" style="width: 5rem"></span>
      <span class="block avatar"></span>
    </div>
    <div class="check-body">
      <span class="block" style="width: min(24rem, 100%); height: 2.25rem"></span>
      <div class="check-grid">
        {#each Array(8) as _}
          <span class="block card"></span>
        {/each}
      </div>
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

  .check-bar {
    display: flex;
    align-items: center;
    gap: 1rem;
    height: var(--navbar-height);
    padding-inline: 1rem;
    background: var(--navbar-bg);
    border-bottom: 1px solid var(--navbar-border);
    box-shadow: var(--navbar-shadow);
  }

  .check-bar img {
    border-radius: 8px;
  }

  .check-bar .avatar {
    width: 2rem;
    height: 2rem;
    margin-inline-start: auto;
    border-radius: 50%;
  }

  .check-body {
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
    padding: 1.5rem 1rem;
  }

  .check-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 1rem;
  }

  .block {
    position: relative;
    display: block;
    height: 1rem;
    overflow: hidden;
    border-radius: 6px;
    background: rgba(100, 116, 139, 0.14);
  }

  .block.card {
    height: 13rem;
    border-radius: 12px;
  }

  .block::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.5), transparent);
    transform: translateX(-100%);
    animation: check-sweep 1.5s linear infinite;
  }

  :global([data-theme='dark']) .block {
    background: rgba(148, 163, 184, 0.16);
  }

  :global([data-theme='dark']) .block::after {
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.08), transparent);
  }

  /* The sweep is mirrored for RTL so it travels in reading order. */
  :global([dir='rtl']) .block::after {
    animation-direction: reverse;
  }

  @keyframes check-sweep {
    to {
      transform: translateX(100%);
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
    .block::after,
    .logo {
      animation: none;
    }
    .bar-fill {
      animation: none;
      width: 100%;
    }
  }
</style>
