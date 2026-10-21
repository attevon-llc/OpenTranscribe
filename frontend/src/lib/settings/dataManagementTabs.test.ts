import { describe, it, expect } from 'vitest';
import {
  dataManagementTabs,
  dataManagementVisible,
  resolveDataTab,
  type DataAccess,
} from './dataManagementTabs';

const ALL: DataAccess = {
  isAdmin: true,
  isSuperAdmin: true,
  retentionCap: true,
  backupCap: true,
  integrityCap: true,
  searchCap: true,
  embeddingCap: true,
};

const ids = (a: DataAccess) => dataManagementTabs(a).map((t) => t.id);

describe('dataManagementTabs', () => {
  it('gives a super_admin every topic, unlocked, in a fixed order', () => {
    expect(dataManagementTabs(ALL)).toEqual([
      { id: 'data-retention', locked: false },
      { id: 'data-cache', locked: false },
      { id: 'data-backup', locked: false },
      { id: 'data-integrity', locked: false },
      { id: 'data-search', locked: false },
      { id: 'data-embeddings', locked: false },
    ]);
  });

  it('shows an admin the backup tab locked, not missing', () => {
    const tabs = dataManagementTabs({ ...ALL, isSuperAdmin: false });
    expect(tabs.find((t) => t.id === 'data-backup')).toEqual({ id: 'data-backup', locked: true });
    expect(tabs.filter((t) => t.locked)).toHaveLength(1);
  });

  it('shows a plain user nothing at all', () => {
    expect(dataManagementTabs({ ...ALL, isAdmin: false, isSuperAdmin: false })).toEqual([]);
    expect(dataManagementVisible({ ...ALL, isAdmin: false, isSuperAdmin: false })).toBe(false);
  });

  it('removes a tab whose capability the deployment lacks', () => {
    expect(ids({ ...ALL, backupCap: false })).not.toContain('data-backup');
    expect(ids({ ...ALL, searchCap: false })).not.toContain('data-search');
    expect(ids({ ...ALL, embeddingCap: false })).not.toContain('data-embeddings');
    expect(ids({ ...ALL, integrityCap: false })).not.toContain('data-integrity');
    // Retention and the media cache share one capability.
    const noRetention = ids({ ...ALL, retentionCap: false });
    expect(noRetention).not.toContain('data-retention');
    expect(noRetention).not.toContain('data-cache');
  });

  it('hides the row when no capability is on', () => {
    const none = {
      ...ALL,
      retentionCap: false,
      backupCap: false,
      integrityCap: false,
      searchCap: false,
      embeddingCap: false,
    };
    expect(dataManagementVisible(none)).toBe(false);
    expect(dataManagementVisible(ALL)).toBe(true);
  });
});

describe('resolveDataTab', () => {
  it('keeps an openable request', () => {
    expect(resolveDataTab('data-integrity', dataManagementTabs(ALL))).toBe('data-integrity');
  });

  it('falls back to the first openable tab when the request is locked', () => {
    const tabs = dataManagementTabs({ ...ALL, isSuperAdmin: false });
    expect(resolveDataTab('data-backup', tabs)).toBe('data-retention');
  });

  it('falls back when the requested tab is absent, and is null with no tabs', () => {
    const tabs = dataManagementTabs({ ...ALL, searchCap: false });
    expect(resolveDataTab('data-search', tabs)).toBe('data-retention');
    expect(resolveDataTab('data-search', [])).toBeNull();
  });

  it('returns the first (locked) tab when every tab is locked so the pane can explain it', () => {
    expect(resolveDataTab('data-backup', [{ id: 'data-backup', locked: true }])).toBe(
      'data-backup'
    );
  });
});
