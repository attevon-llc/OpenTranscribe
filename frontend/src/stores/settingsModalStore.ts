import { writable } from 'svelte/store';

export type SettingsSection =
  // User sections
  | 'profile'
  | 'recording'
  | 'audio-extraction'
  | 'transcription'
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
  | 'engine-settings'
  // System sections (visible to all users)
  | 'system-statistics'
  // Admin sections
  | 'admin-users'
  | 'admin-task-health'
  | 'admin-settings'
  | 'redaction-policy'
  | 'embedding-migration'
  | 'data-integrity'
  | 'retention'
  | 'cache'
  | 'media-sources'
  // Super Admin sections
  | 'authentication'
  | 'audit-logs';

interface SettingsModalState {
  isOpen: boolean;
  activeSection: SettingsSection;
  dirtyState: Record<SettingsSection, boolean>;
}

const initialState: SettingsModalState = {
  isOpen: false,
  activeSection: 'system-statistics',
  dirtyState: {
    profile: false,
    recording: false,
    'audio-extraction': false,
    transcription: false,
    'organization-context': false,
    'speaker-attributes': false,
    download: false,
    'ai-prompts': false,
    'llm-provider': false,
    'auto-labeling': false,
    'search-indexing': false,
    groups: false,
    'asr-provider': false,
    'custom-vocabulary': false,
    'content-redaction': false,
    'engine-settings': false,
    'system-statistics': false,
    'admin-users': false,
    'admin-task-health': false,
    'admin-settings': false,
    'redaction-policy': false,
    'embedding-migration': false,
    'data-integrity': false,
    retention: false,
    cache: false,
    'media-sources': false,
    authentication: false,
    'audit-logs': false,
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
          {} as Record<SettingsSection, boolean>
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
