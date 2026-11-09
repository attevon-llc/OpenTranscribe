/**
 * What every account action shares: the translator, the confirm dialog, the in-flight row
 * marker and the table refetch. Built once by `createAccountActions`.
 */
import type { Writable } from 'svelte/store';
import type { ConfirmDialog } from '$lib/confirmDialog';

export interface AccountRow {
  uuid: string;
  email: string;
  full_name?: string;
  role: string;
  allow_local_fallback?: boolean;
}

export type Translate = (key: string, vars?: Record<string, unknown>) => string;

export interface AccountActionContext {
  confirmDialog: ConfirmDialog;
  onRefresh: () => void;
  pendingUuid: Writable<string | null>;
  tr: Translate;
}

export const nameOf = (user: AccountRow | undefined) => user?.full_name || user?.email || '';
