<!--
  EyeIcon.svelte — show/hide-password eye glyph shared by every password field.
  `visible` is true while the password is shown in clear text.
-->
<script lang="ts">
  export let visible: boolean = false;
</script>

<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  {#if visible}
    <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/>
    <circle cx="12" cy="12" r="3"/>
  {:else}
    <path d="m15 18-.722-3.25"/>
    <path d="m2 2 20 20"/>
    <path d="m9 9-.637 3.181"/>
    <path d="M12.5 5.5c2.13.13 4.16 1.11 5.5 3.5-.274.526-.568 1.016-.891 1.469"/>
    <path d="M2 12s3-7 10-7c1.284 0 2.499.23 3.62.67"/>
    <path d="m18.147 8.476.853 3.524"/>
  {/if}
</svg>
