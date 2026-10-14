<script lang="ts">
  // `inline` is the small icon that sits inside a field label; the default is the card-title icon.
  export let inline = false;
</script>

<span class={inline ? 'inline-info-icon' : 'info-icon'}>
  <svg
    xmlns="http://www.w3.org/2000/svg"
    width={inline ? 12 : 14}
    height={inline ? 12 : 14}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <circle cx="12" cy="12" r="10"></circle>
    <line x1="12" y1="16" x2="12" y2="12"></line>
    <line x1="12" y1="8" x2="12.01" y2="8"></line>
  </svg>
  <span class={inline ? 'inline-tooltip' : 'tooltip'}><slot /></span>
</span>
