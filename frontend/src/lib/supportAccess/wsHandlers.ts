/**
 * The four support-access WebSocket messages (issue #1122), kept out of `$stores/websocket`
 * (already ~1,400 lines). None of them names a MediaFile, so none is quarantine-filtered.
 *
 * Fail closed: outside multi-tenant mode the messages are ignored entirely, mirroring every
 * other support-access surface.
 */
import { get } from 'svelte/store';
import { SUPPORT_ACCESS_EVENT, type SupportAccessEventType } from './events';
import { formatTimeOfDay } from '$lib/utils/formatting';

interface SupportAccessMessage {
  type: SupportAccessEventType;
  data: Record<string, unknown>;
}

const str = (value: unknown): string => (typeof value === 'string' ? value : '');

export async function handleSupportAccessMessage(message: SupportAccessMessage): Promise<void> {
  const [{ capabilities }, { t, locale }, { toastStore }] = await Promise.all([
    import('$stores/capabilities'),
    import('$stores/locale'),
    import('$stores/toast'),
  ]);
  if (get(capabilities).tenancyMode !== 'multi') return;

  const { type, data } = message;
  window.dispatchEvent(new CustomEvent(SUPPORT_ACCESS_EVENT, { detail: { type, data } }));
  const tr = get(t);

  if (type === 'support_access_requested') {
    toastStore.info(tr('supportAccess.notify.requested', { grantee: str(data.grantee_name) }));
  } else if (type === 'support_access_break_glass') {
    const { settingsModalStore } = await import('$stores/settingsModalStore');
    // Persistent (duration 0): a tenant must not miss that platform staff just entered.
    toastStore.warning(
      tr('supportAccess.notify.breakGlass', {
        grantee: str(data.grantee_name),
        target: str(data.target_name),
        time: formatTimeOfDay(str(data.expires_at), get(locale)),
      }),
      0,
      {
        action: {
          label: tr('supportAccess.notify.review'),
          onClick: () => settingsModalStore.open('support-access-requests'),
        },
      }
    );
  } else {
    // decided / revoked: tell the grantee's live session when its own grant stopped being active.
    const { supportSession } = await import('$stores/supportSession');
    if (get(supportSession).grantUuid === str(data.grant_uuid) && str(data.status) !== 'active') {
      await supportSession.end('revoked');
    }
  }
}
