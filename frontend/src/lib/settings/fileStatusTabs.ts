/**
 * Which tabs the "File status" settings row offers.
 *
 * File status (what is happening to your files) and the Task Health Monitor (find and restart stuck
 * work) used to be two rows covering the same ground. They are tabs of one row now: Status for
 * everyone, Recovery for admins on deployments that have the `admin.task_health` capability. A user
 * with only one tab gets no tab strip.
 */

export type StatusTabId = 'status-files' | 'status-recovery';

export interface StatusTab {
  id: StatusTabId;
  locked: boolean;
}

export interface StatusAccess {
  isAdmin: boolean;
  /** `admin.task_health` capability. */
  taskHealthCap: boolean;
}

export function fileStatusTabs(access: StatusAccess): StatusTab[] {
  const tabs: StatusTab[] = [{ id: 'status-files', locked: false }];
  if (access.isAdmin && access.taskHealthCap) tabs.push({ id: 'status-recovery', locked: false });
  return tabs;
}

/** The requested tab when it is offered, otherwise the first one (Status). */
export function resolveStatusTab(requested: StatusTabId, tabs: StatusTab[]): StatusTabId | null {
  const wanted = tabs.find((tab) => tab.id === requested && !tab.locked);
  if (wanted) return wanted.id;
  return (tabs.find((tab) => !tab.locked) ?? tabs[0])?.id ?? null;
}
