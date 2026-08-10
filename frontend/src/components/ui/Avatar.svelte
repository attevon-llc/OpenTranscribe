<script lang="ts">
  import { getInitials } from '$lib/utils/formatting';
  import { t } from '$stores/locale';

  export let name: string | null = null;
  export let email: string = '';
  /** Optional image URL; when set, shows the image instead of initials. */
  export let src: string | null = null;
  export let size: 'sm' | 'md' | 'lg' = 'md';
  /** Optional explicit alt text for the image variant. */
  export let alt: string = '';

  $: initials = getInitials(name, email);
  $: label = alt || name || email || $t('common.userAvatar');
</script>

{#if src}
  <img class={`avatar avatar-${size}`} {src} alt={label} />
{:else}
  <span class={`avatar avatar-${size} avatar-initials`} role="img" aria-label={label}>
    {initials}
  </span>
{/if}

<style>
  .avatar {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    border-radius: 50%;
    object-fit: cover;
    background: var(--primary-color);
    color: white;
    font-weight: 600;
    line-height: 1;
    user-select: none;
  }
  .avatar-sm {
    width: 24px;
    height: 24px;
    font-size: 10px;
  }
  .avatar-md {
    width: 36px;
    height: 36px;
    font-size: 14px;
  }
  .avatar-lg {
    width: 48px;
    height: 48px;
    font-size: 18px;
  }
</style>
