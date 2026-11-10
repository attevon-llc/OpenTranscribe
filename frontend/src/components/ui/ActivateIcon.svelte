<script lang="ts">
  export let size = 14;
  export let strokeLinecap: 'round' | undefined = undefined;
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="2"
  stroke-linecap={strokeLinecap}
  stroke-linejoin={strokeLinecap}
  aria-hidden="true"
>
  <circle cx="12" cy="12" r="3" />
  <circle cx="12" cy="1" r="1" />
  <circle cx="12" cy="23" r="1" />
  <circle cx="4.22" cy="4.22" r="1" />
  <circle cx="19.78" cy="19.78" r="1" />
  <circle cx="1" cy="12" r="1" />
  <circle cx="23" cy="12" r="1" />
  <circle cx="4.22" cy="19.78" r="1" />
  <circle cx="19.78" cy="4.22" r="1" />
</svg>
