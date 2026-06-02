<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  // Import logo asset for proper Vite processing
  import logoBanner from '../../assets/logo-banner.png';

  const dispatch = createEventDispatcher<{ about: void }>();
</script>

<!-- Far Left: OpenTranscribe Logo -->
<div class="navbar-brand">
  <button
    class="logo-link"
    on:click={() => dispatch('about')}
    title={$t('nav.logoTooltip')}
  >
    <img src={logoBanner} alt={$t('nav.logoAlt')} class="logo-banner" />
  </button>
</div>

<style>
  .navbar-brand {
    display: flex;
    align-items: center;
  }

  .logo-link {
    display: flex;
    align-items: center;
    text-decoration: none;
    color: var(--primary-color);
    font-weight: 600;
    font-size: 1.25rem;
    transition: transform 0.2s ease;
    border-radius: 8px;
    padding: 0.25rem;
    background: none;
    border: none;
    cursor: pointer;
  }

  .logo-link:hover {
    transform: scale(1.1);
  }

  .logo-banner {
    height: 36px;
    width: auto;
    object-fit: contain;
    border-radius: 6px;
    transition: inherit;
  }

  /* iPad portrait — smaller logo */
  @media (max-width: 1024px) {
    .logo-banner {
      height: 30px;
    }
  }

  @media (max-width: 768px) {
    .logo-banner {
      height: 28px;
    }
  }

  /* Extra-small screens (iPhone SE, 375px and below) */
  @media (max-width: 480px) {
    .logo-banner {
      height: 24px;
    }
  }
</style>
