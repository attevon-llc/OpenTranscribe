<script lang="ts">
  import { pageStore } from '$stores/pageStore';

  // Track if we're on a full-bleed page (own layout, no wrapper padding)
  $: isFullBleedPage = $pageStore.url.pathname === '/' || ($pageStore.url.pathname as string) === '' || $pageStore.url.pathname === '/search';
</script>

<!-- id="main-content" is the skip link's target (+layout.svelte); tabindex="-1" lets it
     receive focus programmatically even though it is not natively focusable. -->
<main id="main-content" tabindex="-1" class="content {isFullBleedPage ? 'gallery-page' : ''}">
  <slot />
</main>

<style>
  .content {
    padding: 1rem;
    margin-top: var(--content-top, 60px);
  }

  /* Remove padding for gallery page - it handles its own layout */
  :global(.content.gallery-page) {
    padding: 0 !important;
  }

  @media (min-width: 768px) {
    .content {
      padding: 2rem;
    }
  }
</style>
