import { writable } from 'svelte/store';

export type SettingsSection =
  // User sections
  | 'profile'
  | 'recording'
  | 'audio-extraction'
  | 'processing-pipeline'
  | 'transcription'
  | 'speaker-identification'
  | 'organization-context'
  | 'speaker-attributes'
  | 'download'
  | 'ai-prompts'
  | 'llm-provider'
  | 'auto-labeling'
  | 'search-indexing'
  | 'groups'
  | 'asr-provider'
  | 'custom-vocabulary'
  | 'content-redaction'
  | 'chat'
  | 'engine-settings'
  // System sections (visible to all users)
  | 'system-statistics'
  | 'file-status'
  | 'legal'
  | 'my-takedowns'
  // Cloud edition — org-admin sections (gated by audience='org_admin')
  | 'billing'
  | 'usage'
  | 'team'
  | 'org-takedown'
  // Admin sections
  // 'admin-settings' and 'cache' are deliberately absent: neither has had a
  // render block in SettingsModal for some time, so deep-linking to them opened
  // an empty pane. Cache configuration lives inside the 'retention' panel.
  // 'admin-settings' and 'cache' are deliberately absent: neither has had a
  // render block in SettingsModal for some time, so deep-linking to them opened
  // an empty pane. Cache configuration lives inside the 'retention' panel.
  | 'admin-users'
  | 'admin-task-health'
  | 'quarantine'
  | 'takedown-notices'
  | 'chat-admin'
  | 'redaction-policy'
  // The data-storage topics are tabs of 'data-management'; the five ids below survive as aliases.
  | 'data-management'
  | 'embedding-migration'
  | 'data-integrity'
  | 'retention'
  | 'backup'
  | 'media-sources'
  | 'watch-sources'
  | 'support-access'
  | 'support-access-requests'
  // Super Admin sections
  | 'authentication'
  | 'audit-logs';

interface SettingsModalState {
  isOpen: boolean;
  activeSection: SettingsSection;
  /**
   * Sparse: a section appears once something reports it dirty. Alias ids (a legacy id that now
   * opens a tab of another row) are never tracked here; read a row's dirt through its own id.
   */
  dirtyState: Partial<Record<SettingsSection, boolean>>;
}

const initialState: SettingsModalState = {
  isOpen: false,
  activeSection: 'system-statistics',
  dirtyState: {
    profile: false,
    recording: false,
    'audio-extraction': false,
    'processing-pipeline': false,
    transcription: false,
    'speaker-identification': false,
    'organization-context': false,
    download: false,
    'ai-prompts': false,
    'llm-provider': false,
    'auto-labeling': false,
    groups: false,
    'content-redaction': false,
    chat: false,
    'chat-admin': false,
    'redaction-policy': false,
    retention: false,
    backup: false,
    'media-sources': false,
    'watch-sources': false,
  },
};

function createSettingsModalStore() {
  const { subscribe, set, update } = writable<SettingsModalState>(initialState);

  return {
    subscribe,
    open: (section?: SettingsSection) => {
      update((state) => ({
        ...state,
        isOpen: true,
        activeSection: section || 'system-statistics',
      }));
    },
    close: () => {
      update((state) => ({
        ...state,
        isOpen: false,
        activeSection: 'system-statistics',
      }));
    },
    setActiveSection: (section: SettingsSection) => {
      update((state) => ({
        ...state,
        activeSection: section,
      }));
    },
    setDirty: (section: SettingsSection, isDirty: boolean) => {
      update((state) => ({
        ...state,
        dirtyState: {
          ...state.dirtyState,
          [section]: isDirty,
        },
      }));
    },
    clearDirty: (section: SettingsSection) => {
      update((state) => ({
        ...state,
        dirtyState: {
          ...state.dirtyState,
          [section]: false,
        },
      }));
    },
    clearAllDirty: () => {
      update((state) => ({
        ...state,
        dirtyState: Object.keys(state.dirtyState).reduce(
          (acc, key) => {
            acc[key as SettingsSection] = false;
            return acc;
          },
          {} as Partial<Record<SettingsSection, boolean>>
        ),
      }));
    },
    hasAnyDirty: (state: SettingsModalState): boolean => {
      return Object.values(state.dirtyState).some((isDirty) => isDirty);
    },
    reset: () => set(initialState),
  };
}

export const settingsModalStore = createSettingsModalStore();
