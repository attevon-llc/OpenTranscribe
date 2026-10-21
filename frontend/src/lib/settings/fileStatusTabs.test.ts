import { describe, it, expect } from 'vitest';
import { fileStatusTabs, resolveStatusTab, type StatusAccess } from './fileStatusTabs';

const ADMIN: StatusAccess = { isAdmin: true, taskHealthCap: true };

describe('fileStatusTabs', () => {
  it('gives an admin Status then Recovery, both open', () => {
    expect(fileStatusTabs(ADMIN)).toEqual([
      { id: 'status-files', locked: false },
      { id: 'status-recovery', locked: false },
    ]);
  });

  it('gives a plain user the Status tab only', () => {
    expect(fileStatusTabs({ isAdmin: false, taskHealthCap: true })).toEqual([
      { id: 'status-files', locked: false },
    ]);
  });

  it('removes Recovery when the deployment lacks the task-health capability', () => {
    expect(fileStatusTabs({ ...ADMIN, taskHealthCap: false }).map((t) => t.id)).toEqual([
      'status-files',
    ]);
  });
});

describe('resolveStatusTab', () => {
  it('honours a request for an openable tab', () => {
    expect(resolveStatusTab('status-recovery', fileStatusTabs(ADMIN))).toBe('status-recovery');
  });

  it('falls back to Status when Recovery is not offered (alias opened by a non-admin)', () => {
    const user = fileStatusTabs({ isAdmin: false, taskHealthCap: true });
    expect(resolveStatusTab('status-recovery', user)).toBe('status-files');
  });

  it('is null when there are no tabs', () => {
    expect(resolveStatusTab('status-files', [])).toBeNull();
  });
});
