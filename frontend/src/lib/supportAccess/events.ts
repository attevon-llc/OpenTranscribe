/**
 * The window event `$stores/websocket` raises for the four support-access push messages
 * (issue #1122), so panels can reload without importing the socket store.
 *
 * `type` is the backend WebSocket type; `data` is its payload.
 */
export const SUPPORT_ACCESS_EVENT = 'support-access-event';

export type SupportAccessEventType =
  | 'support_access_requested'
  | 'support_access_break_glass'
  | 'support_access_decided'
  | 'support_access_revoked';

export interface SupportAccessEventDetail {
  type: SupportAccessEventType;
  data: Record<string, unknown>;
}
