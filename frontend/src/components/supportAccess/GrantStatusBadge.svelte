<script lang="ts">
  import { SUPPORT_STATUS_KEYS } from '$lib/i18n/keyMaps';
  import { t } from '$stores/locale';
  import StatusChip from './StatusChip.svelte';
  import type { GrantStatus } from '$lib/api/supportAccess';

  export let status: GrantStatus;

  type Tone = 'success' | 'warning' | 'error' | 'info' | 'neutral';
  // A Record, not a ternary: a status added later must be a type error here, not a
  // silent fall-through to some other status's colour.
  const TONE: Record<GrantStatus, Tone> = {
    pending: 'warning',
    active: 'success',
    denied: 'error',
    expired: 'neutral',
    revoked: 'error',
    lapsed: 'neutral',
  };
</script>

<StatusChip tone={TONE[status]}>{$t(SUPPORT_STATUS_KEYS[status])}</StatusChip>
