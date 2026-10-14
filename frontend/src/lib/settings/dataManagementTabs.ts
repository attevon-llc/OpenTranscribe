/**
 * Which tabs the "Data & Storage" settings section offers.
 *
 * File retention, the derived-media cache, scheduled database backups, data integrity, the search
 * index and the speaker-embedding system used to be five separate System rows (six panels). They
 * are all about the state of the deployment's stored data, so they are tabs of one row now.
 *
 * Every tab is admin; backups are super_admin. A tab the user's tier cannot open is returned
 * `locked` rather than dropped (the Settings convention for a privilege the user merely lacks),
 * while a capability the deployment does not have removes the tab entirely.
 */

export type DataTabId =
  | 'data-retention'
  | 'data-cache'
  | 'data-backup'
  | 'data-integrity'
  | 'data-search'
  | 'data-embeddings';

export interface DataTab {
  id: DataTabId;
  locked: boolean;
}

export interface DataAccess {
  isAdmin: boolean;
  isSuperAdmin: boolean;
  /** `admin.retention` capability (retention and the media cache share it). */
  retentionCap: boolean;
  /** `admin.backup` capability. */
  backupCap: boolean;
  /** `admin.data_integrity` capability. */
  integrityCap: boolean;
  /** `admin.search_indexing` capability. */
  searchCap: boolean;
  /** `admin.embedding_migration` capability. */
  embeddingCap: boolean;
}

export function dataManagementTabs(access: DataAccess): DataTab[] {
  if (!access.isAdmin) return [];
  const tabs: DataTab[] = [];
  if (access.retentionCap) {
    tabs.push({ id: 'data-retention', locked: false });
    tabs.push({ id: 'data-cache', locked: false });
  }
  if (access.backupCap) tabs.push({ id: 'data-backup', locked: !access.isSuperAdmin });
  if (access.integrityCap) tabs.push({ id: 'data-integrity', locked: false });
  if (access.searchCap) tabs.push({ id: 'data-search', locked: false });
  if (access.embeddingCap) tabs.push({ id: 'data-embeddings', locked: false });
  return tabs;
}

/** Whether the sidebar should list the section at all. */
export function dataManagementVisible(access: DataAccess): boolean {
  return dataManagementTabs(access).length > 0;
}

/**
 * The tab to show for a requested one: the request when it is openable, otherwise the first
 * openable tab, otherwise (every tab locked) the first tab so the pane can explain the lock.
 */
export function resolveDataTab(requested: DataTabId, tabs: DataTab[]): DataTabId | null {
  const wanted = tabs.find((tab) => tab.id === requested && !tab.locked);
  if (wanted) return wanted.id;
  return (tabs.find((tab) => !tab.locked) ?? tabs[0])?.id ?? null;
}
