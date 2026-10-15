<script lang="ts">
  import InfoTip from './InfoTip.svelte';
  export let label: string | undefined = undefined;
  export let inline = false;
</script>

<div>
  <button type="button" data-testid="outside">outside</button>
  <InfoTip {label} {inline}>
    <strong>Heading</strong>
    <ul>
      <li>one</li>
      <li>two</li>
    </ul>
  </InfoTip>
</div>
