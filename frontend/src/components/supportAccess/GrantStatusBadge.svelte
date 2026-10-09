<script lang="ts">
  import { SUPPORT_STATUS_KEYS } from '$lib/i18n/keyMaps';
  import { t } from '$stores/locale';
  import Badge from '$components/ui/Badge.svelte';
  import type { GrantStatus } from '$lib/api/supportAccess';

  export let status: GrantStatus;

  type Variant = 'default' | 'success' | 'warning' | 'error' | 'info';
  // A Record, not a ternary: a status added later must be a type error here, not a
  // silent fall-through to some other status's colour.
  const VARIANT: Record<GrantStatus, Variant> = {
    pending: 'warning',
    active: 'success',
    denied: 'error',
    expired: 'default',
    revoked: 'error',
    lapsed: 'default',
  };
</script>

<Badge variant={VARIANT[status]}>{$t(SUPPORT_STATUS_KEYS[status])}</Badge>
