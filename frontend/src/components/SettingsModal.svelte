<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { page } from '$app/stores';
  import { user as userStore, authStore, fetchUserInfo } from '$stores/auth';
  import { settingsModalStore, type SettingsSection } from '$stores/settingsModalStore';
  import { toastStore } from '$stores/toast';
  import axiosInstance from '$lib/axios';
  import { UserSettingsApi, RecordingSettingsHelper, type RecordingSettings } from '$lib/api/userSettings';

  // Import settings components
  import LLMSettings from '$components/settings/LLMSettings.svelte';
  import PromptSettings from '$components/settings/PromptSettings.svelte';
  import AudioExtractionSettings from '$components/settings/AudioExtractionSettings.svelte';
  import TranscriptionSettings from '$components/settings/TranscriptionSettings.svelte';
  import OrganizationContextSettings from '$components/settings/OrganizationContextSettings.svelte';
  import DownloadSettings from '$components/settings/DownloadSettings.svelte';
  import MediaSourcesSettings from '$components/settings/MediaSourcesSettings.svelte';
  import RetrySettings from '$components/settings/RetrySettings.svelte';
  import LanguageSettings from '$components/settings/LanguageSettings.svelte';
  import SecuritySettings from '$components/settings/SecuritySettings.svelte';
  import SearchSettings from '$components/settings/SearchSettings.svelte';
  import GroupsSettings from '$components/settings/GroupsSettings.svelte';
  import DataIntegritySettings from '$components/settings/DataIntegritySettings.svelte';
  import EmbeddingConsistencySettings from '$components/settings/EmbeddingConsistencySettings.svelte';
  import EmbeddingMigrationSettings from '$components/settings/EmbeddingMigrationSettings.svelte';
  import RetentionSettings from '$components/settings/RetentionSettings.svelte';
  import SpeakerAttributeSettings from '$components/settings/SpeakerAttributeSettings.svelte';
  import AutoLabelSettings from '$components/settings/AutoLabelSettings.svelte';
  import AuthenticationSettings from '$components/settings/AuthenticationSettings.svelte';
  import AccountStatusDashboard from '$components/settings/AccountStatusDashboard.svelte';
  import AuditLogViewer from '$components/settings/AuditLogViewer.svelte';
  import ASRSettings from '$components/settings/ASRSettings.svelte';
  import EngineSettings from '$components/settings/EngineSettings.svelte';
  import CustomVocabularySettings from '$components/settings/CustomVocabularySettings.svelte';
  import UserManagementTable from '$components/UserManagementTable.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import ProcessingDetailsModal from '$components/settings/ProcessingDetailsModal.svelte';

  // Import i18n
  import { t } from '$stores/locale';
  import Spinner from './ui/Spinner.svelte';

  // Modal state
  let modalElement: HTMLElement;
  let showCloseConfirmation = false;

  // Settings state
  $: isOpen = $settingsModalStore.isOpen;
  $: activeSection = $settingsModalStore.activeSection;

  // Close modal when the user navigates to a different page
  let _prevPath = '';
  $: {
    const newPath = $page.url.pathname;
    if (isOpen && _prevPath && newPath !== _prevPath) {
      closeModal();
    }
    _prevPath = newPath;
  }
  $: isAdmin = $userStore?.role === 'admin' || $userStore?.role === 'super_admin';
  $: isSuperAdmin = $userStore?.role === 'super_admin';
  $: isLocalUser = $userStore?.auth_type === 'local';

  // User Profile section
  let fullName = '';
  let email = '';
  let profileChanged = false;
  let profileLoading = false;

  // Password section
  let currentPassword = '';
  let newPassword = '';
  let confirmPassword = '';
  let passwordChanged = false;
  let passwordLoading = false;
  let showCurrentPassword = false;
  let showNewPassword = false;
  let showConfirmPassword = false;

  // Recording settings section
  let maxRecordingDuration = 120;
  let recordingQuality: 'standard' | 'high' | 'maximum' = 'high';
  let autoStopEnabled = true;
  let recordingSettingsChanged = false;
  let recordingSettingsLoading = false;

  // Admin Users section
  let users: any[] = [];
  let usersLoading = false;

  // Admin Stats section
  let stats: any = {
    users: { total: 0, new: 0 },
    files: { total: 0, new: 0, total_duration: 0, segments: 0 },
    tasks: {
      total: 0,
      pending: 0,
      running: 0,
      completed: 0,
      failed: 0,
      success_rate: 0,
      avg_processing_time: 0,
      recent: []
    },
    speakers: { total: 0, avg_per_file: 0 },
    models: {
      whisper: { name: 'N/A', description: 'N/A', purpose: 'N/A' },
      diarization: { name: 'N/A', description: 'N/A', purpose: 'N/A' }
    },
    system: {
      cpu: { total_percent: '0%', per_cpu: [], logical_cores: 0, physical_cores: 0 },
      memory: { total: '0 B', available: '0 B', used: '0 B', percent: '0%' },
      disk: { total: '0 B', used: '0 B', free: '0 B', percent: '0%' },
      gpus: [{ available: false, name: 'N/A', memory_total: 'N/A', memory_used: 'N/A', memory_free: 'N/A', memory_percent: 'N/A', utilization_percent: 'N/A', temperature_celsius: null }],
      uptime: 'Unknown',
      platform: 'Unknown',
      python_version: 'Unknown'
    },
    throughput: { total_completed: 0, last_1h: 0, last_3h: 0, rate_1h: 0, rate_3h: 0 },
    eta: { remaining: 0, files_per_hour: 0, hours_remaining: null, est_completion: null },
    file_timing: { files: 0, avg_secs: 0, min_secs: 0, max_secs: 0, avg_mins: 0 },
    queues: { gpu: 0, download: 0, nlp: 0, embedding: 0, cpu: 0, utility: 0, total: 0 }
  };
  let showProcessingDetails = false;
  let processingDetailsSection = 'performance';
  let statsLoading = false;
  let statsRefreshing = false;
  let statsInitialLoaded = false;
  let gpuRetryScheduled = false;
  let currentGpuIndex = 0;
  $: activeGpu = stats.system?.gpus?.[currentGpuIndex] ?? stats.system?.gpus?.[0];
  $: gpuCount = stats.system?.gpus?.length ?? 1;

  // Search index status (for system stats card)
  let searchIndexStatus: { indexed_files: number; total_files: number; pending_files: number; in_progress: boolean; current_model: string } | null = null;
  let searchHealthStatus: Record<string, { status: string; doc_count: number }> | null = null;

  // Admin Task Health section
  let taskHealthData: any = null;
  let taskHealthLoading = false;
  let showConfirmModal = false;
  let confirmModalTitle = '';
  let confirmModalMessage = '';
  let confirmCallback: (() => void) | null = null;

  // Define sidebar sections
  $: sidebarSections = [
    {
      title: $t('settings.sections.system'),
      items: [
        { id: 'system-statistics' as SettingsSection, label: $t('settings.statistics.title'), icon: 'chart' }
      ]
    },
    ...(isAdmin ? [
      {
        title: $t('settings.sections.administration'),
        items: [
          ...(isSuperAdmin ? [
            { id: 'audit-logs' as SettingsSection, label: $t('settings.auditLog.navLabel'), icon: 'list' },
            { id: 'authentication' as SettingsSection, label: $t('settings.authentication.title'), icon: 'key' }
          ] : []),
          { id: 'admin-users' as SettingsSection, label: $t('settings.users.title'), icon: 'users' }
        ]
      },
      {
        title: $t('settings.sections.systemManagement'),
        items: [
          { id: 'data-integrity' as SettingsSection, label: $t('settings.dataIntegrity.title'), icon: 'shield' },
          { id: 'retention' as SettingsSection, label: $t('settings.retention.title'), icon: 'clock' },
          { id: 'search-indexing' as SettingsSection, label: $t('settings.searchIndexing.title'), icon: 'search' },
          { id: 'embedding-migration' as SettingsSection, label: $t('settings.embeddingMigration.title'), icon: 'database' },
          { id: 'admin-task-health' as SettingsSection, label: $t('settings.taskHealth.title'), icon: 'health' }
        ]
      }
    ] : []),
    {
      title: $t('settings.sections.account'),
      items: [
        { id: 'groups' as SettingsSection, label: $t('groups.title'), icon: 'group' },
        { id: 'profile' as SettingsSection, label: $t('settings.profile.title'), icon: 'user' }
      ]
    },
    {
      title: $t('settings.sections.transcriptionAi'),
      items: [
        { id: 'ai-prompts' as SettingsSection, label: $t('settings.aiPrompts.title'), icon: 'message' },
        { id: 'asr-provider' as SettingsSection, label: $t('settings.asrProvider.title'), icon: 'mic' },
        ...(isAdmin ? [{ id: 'engine-settings' as SettingsSection, label: $t('settings.engineSettings.title'), icon: 'cpu' }] : []),
        { id: 'auto-labeling' as SettingsSection, label: $t('autoLabel.title'), icon: 'tag' },
        { id: 'custom-vocabulary' as SettingsSection, label: $t('settings.customVocabulary.title'), icon: 'list' },
        { id: 'llm-provider' as SettingsSection, label: $t('settings.llmProvider.title'), icon: 'brain' },
        { id: 'organization-context' as SettingsSection, label: $t('settings.orgContext.title'), icon: 'briefcase' },
        { id: 'speaker-attributes' as SettingsSection, label: $t('settings.speakerAttributes.navTitle'), icon: 'user' },
        { id: 'transcription' as SettingsSection, label: $t('settings.transcription.title'), icon: 'waveform' }
      ]
    },
    {
      title: $t('settings.sections.mediaOutput'),
      items: [
        { id: 'audio-extraction' as SettingsSection, label: $t('settings.audioExtraction.title'), icon: 'file-audio' },
        { id: 'media-sources' as SettingsSection, label: $t('settings.mediaSources.title'), icon: 'link' },
        { id: 'recording' as SettingsSection, label: $t('settings.recording.title'), icon: 'mic' },
        { id: 'download' as SettingsSection, label: $t('settings.download.title'), icon: 'download' }
      ]
    }
  ];

  // Reactive profile change detection
  $: if ($authStore.user) {
    profileChanged = $authStore.user.full_name !== fullName;
  }

  // Reactive password change detection
  $: {
    passwordChanged = !!(currentPassword || newPassword || confirmPassword);
  }

  // Combined profile dirty state (profile changes OR password changes)
  $: {
    const isDirty = profileChanged || passwordChanged;
    settingsModalStore.setDirty('profile', isDirty);
  }

  // Reactive recording settings change detection
  $: {
    settingsModalStore.setDirty('recording', recordingSettingsChanged);
  }

  // Reactive user data update when authStore changes or modal opens
  $: if ($authStore.user && isOpen) {
    fullName = $authStore.user.full_name || '';
    email = $authStore.user.email || '';
  }

  // Load data when modal opens
  $: if (isOpen && !profileLoading && !recordingSettingsLoading) {
    // Only reload if we haven't loaded yet or data is stale
    if (!fullName && $authStore.user) {
      fullName = $authStore.user.full_name || '';
      email = $authStore.user.email || '';
    }
  }

  onMount(() => {
    // Initialize user data
    if ($authStore.user) {
      fullName = $authStore.user.full_name || '';
      email = $authStore.user.email || '';
    }

    // Load recording settings
    loadRecordingSettings();

    // Load statistics for any user
    if (activeSection === 'system-statistics') {
      loadStats();
    }

    // Load admin data if admin
    if (isAdmin) {
      if (activeSection === 'admin-users') {
        loadAdminUsers();
      } else if (activeSection === 'admin-task-health') {
        loadTaskHealth();
      }
    }

    // Add escape key listener
    document.addEventListener('keydown', handleKeyDown);
    window.addEventListener('gpu-stats-updated', handleGpuStatsEvent);
    window.addEventListener('reindex-complete', handleReindexCompleteStats);
  });

  onDestroy(() => {
    document.removeEventListener('keydown', handleKeyDown);
    window.removeEventListener('gpu-stats-updated', handleGpuStatsEvent);
    window.removeEventListener('reindex-complete', handleReindexCompleteStats);
    // Re-enable scroll when component is destroyed
    document.documentElement.style.overflow = '';
    document.body.style.overflow = '';
  });

  // Track previous open state to detect when modal opens
  let previousOpenState = false;

  // Prevent body scroll when modal is open and load initial data
  $: {
    if (isOpen && !previousOpenState) {
      // Modal just opened — prevent background scroll
      document.documentElement.style.overflow = 'hidden';
      document.body.style.overflow = 'hidden';

      // Load data for the active section when modal opens
      if (activeSection === 'system-statistics') {
        loadStats();
      } else if (activeSection === 'admin-users' && isAdmin) {
        loadAdminUsers();
      } else if (activeSection === 'admin-task-health' && isAdmin) {
        loadTaskHealth();
      }

      previousOpenState = true;
    } else if (!isOpen && previousOpenState) {
      // Modal just closed — restore background scroll
      document.documentElement.style.overflow = '';
      document.body.style.overflow = '';
      previousOpenState = false;
    }
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === 'Escape' && isOpen) {
      attemptClose();
    }
  }

  function handleBackdropClick(event: MouseEvent) {
    if (event.target === event.currentTarget) {
      attemptClose();
    }
  }

  function attemptClose() {
    const hasUnsavedChanges = settingsModalStore.hasAnyDirty($settingsModalStore);
    if (hasUnsavedChanges) {
      showCloseConfirmation = true;
    } else {
      closeModal();
    }
  }

  function closeModal() {
    settingsModalStore.close();
    showCloseConfirmation = false;
    resetAllForms();
  }

  function forceClose() {
    showCloseConfirmation = false;
    closeModal();
  }

  function resetAllForms() {
    // Reset profile
    if ($authStore.user) {
      fullName = $authStore.user.full_name || '';
      email = $authStore.user.email || '';
    }

    // Reset password
    currentPassword = '';
    newPassword = '';
    confirmPassword = '';
    showCurrentPassword = false;
    showNewPassword = false;
    showConfirmPassword = false;

    // Reset recording settings
    loadRecordingSettings();

    // Clear all dirty states
    settingsModalStore.clearAllDirty();
  }

  function handleGpuStatsEvent(event: Event) {
    const gpuData = (event as CustomEvent).detail;
    if (gpuData && stats?.system) {
      const gpus = Array.isArray(gpuData) ? gpuData : [gpuData];
      // Clamp index in case GPU count decreased
      if (currentGpuIndex >= gpus.length) currentGpuIndex = 0;
      stats = { ...stats, system: { ...stats.system, gpus } };
    }
  }

  function switchSection(sectionId: SettingsSection) {
    settingsModalStore.setActiveSection(sectionId);

    // Load data for specific sections on navigation.
    // GPU stats refresh via WebSocket broadcast (every 5 min) — no polling needed.
    if (sectionId === 'system-statistics') {
      loadStats();
    } else if (sectionId === 'admin-users') {
      loadAdminUsers();
    } else if (sectionId === 'admin-task-health') {
      loadTaskHealth();
    }
  }

  // Profile functions
  async function updateProfile() {
    profileLoading = true;

    try {
      const response = await axiosInstance.put('/users/me', {
        full_name: fullName
      });

      authStore.setUser(response.data);

      toastStore.success($t('settings.toast.profileUpdated'));
      profileChanged = false;
      settingsModalStore.clearDirty('profile');

      await fetchUserInfo();
    } catch (err: any) {
      console.error('Error updating profile:', err);
      const message = err.response?.data?.detail || $t('settings.toast.profileUpdateFailed');
      toastStore.error(message);
    } finally {
      profileLoading = false;
    }
  }

  // Password functions
  async function updatePassword() {
    passwordLoading = true;

    // Validation
    if (!currentPassword || !newPassword || !confirmPassword) {
      toastStore.error($t('settings.toast.passwordFieldsRequired'));
      passwordLoading = false;
      return;
    }

    if (newPassword !== confirmPassword) {
      toastStore.error($t('settings.toast.passwordsNotMatch'));
      passwordLoading = false;
      return;
    }

    if (newPassword.length < 8) {
      toastStore.error($t('settings.toast.passwordTooShort'));
      passwordLoading = false;
      return;
    }

    try {
      await axiosInstance.put('/users/me', {
        password: newPassword,
        current_password: currentPassword
      });

      toastStore.success($t('settings.toast.passwordUpdated'));

      // Clear password fields
      currentPassword = '';
      newPassword = '';
      confirmPassword = '';
      showCurrentPassword = false;
      showNewPassword = false;
      showConfirmPassword = false;
      passwordChanged = false;
      // Note: dirty state is managed reactively based on profileChanged || passwordChanged
    } catch (err: any) {
      console.error('Error updating password:', err);
      const message = err.response?.data?.detail || $t('settings.toast.passwordUpdateFailed');
      toastStore.error(message);
    } finally {
      passwordLoading = false;
    }
  }

  // Recording settings functions
  async function loadRecordingSettings() {
    recordingSettingsLoading = true;
    try {
      const settings = await UserSettingsApi.getRecordingSettings();
      maxRecordingDuration = settings.max_recording_duration;
      recordingQuality = settings.recording_quality;
      autoStopEnabled = settings.auto_stop_enabled;
      recordingSettingsChanged = false;
    } catch (err: any) {
      console.error('Error loading recording settings:', err);
      const message = err.response?.data?.detail || $t('settings.toast.recordingSettingsSaveFailed');
      toastStore.error(message);
    } finally {
      recordingSettingsLoading = false;
    }
  }

  function handleRecordingSettingsChange() {
    recordingSettingsChanged = true;
    settingsModalStore.setDirty('recording', true);
  }

  async function saveRecordingSettings() {
    recordingSettingsLoading = true;

    // Validate settings
    const settingsToValidate: RecordingSettings = {
      max_recording_duration: maxRecordingDuration,
      recording_quality: recordingQuality,
      auto_stop_enabled: autoStopEnabled
    };

    const validationErrors = RecordingSettingsHelper.validateSettings(settingsToValidate);
    if (validationErrors.length > 0) {
      toastStore.error(validationErrors[0]);
      recordingSettingsLoading = false;
      return;
    }

    try {
      await UserSettingsApi.updateRecordingSettings(settingsToValidate);
      toastStore.success($t('settings.toast.recordingSettingsSaved'));
      recordingSettingsChanged = false;
      settingsModalStore.clearDirty('recording');
    } catch (err: any) {
      console.error('Error saving recording settings:', err);
      const message = err.response?.data?.detail || $t('settings.toast.recordingSettingsSaveFailed');
      toastStore.error(message);
    } finally {
      recordingSettingsLoading = false;
    }
  }

  async function resetRecordingSettings() {
    recordingSettingsLoading = true;

    try {
      await UserSettingsApi.resetRecordingSettings();
      await loadRecordingSettings();
      toastStore.success($t('settings.toast.recordingSettingsReset'));
      recordingSettingsChanged = false;
      settingsModalStore.clearDirty('recording');
    } catch (err: any) {
      console.error('Error resetting recording settings:', err);
      const message = err.response?.data?.detail || $t('settings.toast.recordingSettingsResetFailed');
      toastStore.error(message);
    } finally {
      recordingSettingsLoading = false;
    }
  }

  // Admin functions
  async function loadAdminUsers(showLoading = true) {
    // Only show loading spinner on initial load, not on refresh
    if (showLoading) {
      usersLoading = true;
    }

    try {
      const response = await axiosInstance.get('/admin/users');
      users = response.data;
    } catch (err: any) {
      console.error('Error loading admin users:', err);
      const message = err.response?.data?.detail || $t('settings.toast.usersLoadFailed');
      toastStore.error(message);
    } finally {
      if (showLoading) {
        usersLoading = false;
      }
    }
  }

  async function refreshAdminUsers() {
    // Silent refresh - don't show loading spinner to reduce flicker
    await loadAdminUsers(false);
  }

  async function recoverUserFiles(userId: string) {
    try {
      await axiosInstance.post(`/tasks/system/recover-user-files/${userId}`);
      toastStore.success($t('settings.toast.userRecoveryInitiated'));
    } catch (err: any) {
      console.error('Error recovering user files:', err);
      const message = err.response?.data?.detail || $t('settings.toast.userRecoveryFailed');
      toastStore.error(message);
    }
  }

  async function loadStats() {
    if (statsInitialLoaded) {
      statsRefreshing = true;
    } else {
      statsLoading = true;
    }

    try {
      const [statsRes, indexRes, healthRes] = await Promise.all([
        axiosInstance.get('/system/stats'),
        axiosInstance.get('/search/reindex/status').catch(() => null),
        axiosInstance.get('/search/index-health').catch(() => null),
      ]);

      stats = statsRes.data;
      statsInitialLoaded = true;

      if (indexRes?.data) searchIndexStatus = indexRes.data;
      if (healthRes?.data) searchHealthStatus = healthRes.data;

      // Auto-retry once if GPU stats are loading
      if (statsRes.data?.system?.gpus?.[0]?.loading && !gpuRetryScheduled) {
        gpuRetryScheduled = true;
        setTimeout(() => {
          gpuRetryScheduled = false;
          loadStats();
        }, 5000);
      }
    } catch (err: any) {
      console.error('Error loading stats:', err);
      const message = err.response?.data?.detail || $t('settings.toast.statisticsLoadFailed');
      toastStore.error(message);
    } finally {
      statsLoading = false;
      statsRefreshing = false;
    }
  }

  async function refreshStats() {
    await loadStats();
  }

  function handleReindexCompleteStats() {
    // Refresh system stats after reindex completes (e.g., new embedding model)
    if (statsInitialLoaded) {
      loadStats();
    }
  }

  function openProcessingDetails(section: string) {
    processingDetailsSection = section;
    showProcessingDetails = true;
  }

  async function loadTaskHealth() {
    taskHealthLoading = true;

    try {
      const response = await axiosInstance.get('/tasks/system/health');
      taskHealthData = response.data;
    } catch (err: any) {
      console.error('Error loading task health:', err);
      const message = err.response?.data?.detail || $t('settings.toast.taskHealthLoadFailed');
      toastStore.error(message);
    } finally {
      taskHealthLoading = false;
    }
  }

  async function refreshTaskHealth() {
    await loadTaskHealth();
  }

  function showConfirmation(title: string, message: string, callback: () => void) {
    confirmModalTitle = title;
    confirmModalMessage = message;
    confirmCallback = callback;
    showConfirmModal = true;
  }

  function handleConfirmModalConfirm() {
    showConfirmModal = false;
    if (confirmCallback) {
      confirmCallback();
      confirmCallback = null;
    }
  }

  function handleConfirmModalCancel() {
    showConfirmModal = false;
    confirmCallback = null;
  }

  async function recoverStuckTasks() {
    showConfirmation(
      $t('settings.taskHealth.recoverStuck'),
      $t('settings.taskHealth.confirmRecoverStuck'),
      async () => {
        try {
          await axiosInstance.post('/tasks/recover-stuck-tasks');
          toastStore.success($t('settings.toast.stuckTasksRecoveryInitiated'));
          await refreshTaskHealth();
        } catch (err: any) {
          console.error('Error recovering stuck tasks:', err);
          const message = err.response?.data?.detail || $t('settings.toast.stuckTasksRecoveryFailed');
          toastStore.error(message);
        }
      }
    );
  }

  async function fixInconsistentFiles() {
    showConfirmation(
      $t('settings.taskHealth.fixInconsistent'),
      $t('settings.taskHealth.confirmFixInconsistent'),
      async () => {
        try {
          await axiosInstance.post('/tasks/fix-inconsistent-files');
          toastStore.success($t('settings.toast.inconsistentFilesFixInitiated'));
          await refreshTaskHealth();
        } catch (err: any) {
          console.error('Error fixing inconsistent files:', err);
          const message = err.response?.data?.detail || $t('settings.toast.inconsistentFilesFixFailed');
          toastStore.error(message);
        }
      }
    );
  }

  async function startupRecovery() {
    showConfirmation(
      $t('settings.taskHealth.startupRecovery'),
      $t('settings.taskHealth.confirmStartupRecovery'),
      async () => {
        try {
          await axiosInstance.post('/tasks/system/startup-recovery');
          toastStore.success($t('settings.toast.startupRecoveryInitiated'));
          await refreshTaskHealth();
        } catch (err: any) {
          console.error('Error running startup recovery:', err);
          const message = err.response?.data?.detail || $t('settings.toast.startupRecoveryFailed');
          toastStore.error(message);
        }
      }
    );
  }

  async function recoverAllUserFiles() {
    showConfirmation(
      $t('settings.taskHealth.recoverAllUsers'),
      $t('settings.taskHealth.confirmRecoverAllUsers'),
      async () => {
        try {
          await axiosInstance.post('/tasks/system/recover-all-user-files');
          toastStore.success($t('settings.toast.allUserFilesRecoveryInitiated'));
          await refreshTaskHealth();
        } catch (err: any) {
          console.error('Error recovering all user files:', err);
          const message = err.response?.data?.detail || $t('settings.toast.allUserFilesRecoveryFailed');
          toastStore.error(message);
        }
      }
    );
  }

  async function retryTask(taskId: number) {
    try {
      await axiosInstance.post(`/tasks/system/recover-task/${taskId}`);
      toastStore.success($t('settings.toast.taskRetryInitiated'));
      await refreshTaskHealth();
    } catch (err: any) {
      console.error('Error retrying task:', err);
      const message = err.response?.data?.detail || $t('settings.toast.taskRetryFailed');
      toastStore.error(message);
    }
  }

  async function retryFile(fileId: string) {
    try {
      await axiosInstance.post(`/tasks/retry/${fileId}`);
      toastStore.success($t('settings.toast.fileRetryInitiated'));
      await refreshTaskHealth();
    } catch (err: any) {
      console.error('Error retrying file:', err);
      const message = err.response?.data?.detail || $t('settings.toast.fileRetryFailed');
      toastStore.error(message);
    }
  }

  // AI settings change handlers
  function onAISettingsChange() {
    // Handler for AI settings changes - can be extended for additional logic
  }

  // Helper function for formatting time
  function formatTime(seconds: number): string {
    if (!seconds) return '0s';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    let result = '';
    if (hours > 0) result += `${hours}h `;
    if (minutes > 0 || hours > 0) result += `${minutes}m `;
    result += `${secs}s`;
    return result;
  }

  // Helper function for formatting status text
  // Uses compact symbols on small screens
  const isMobileView = typeof window !== 'undefined' && window.innerWidth < 768;

  function formatStatus(status: string): string {
    if (isMobileView) {
      const compactMap: Record<string, string> = {
        'completed': '✓',
        'success': '✓',
        'processing': '...',
        'in_progress': '...',
        'pending': '--',
        'error': '✗',
        'failed': '✗',
      };
      return compactMap[status.toLowerCase()] || status.slice(0, 4);
    }
    const statusMap: Record<string, string> = {
      'completed': $t('common.completed'),
      'processing': $t('common.processing'),
      'pending': $t('common.pending'),
      'error': $t('common.error'),
      'failed': $t('fileStatus.failed'),
      'in_progress': $t('fileStatus.inProgress'),
      'success': $t('common.success'),
    };
    return statusMap[status.toLowerCase()] || status.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }
</script>

{#if isOpen}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="settings-modal-backdrop"
    on:click={handleBackdropClick}
    on:wheel|preventDefault|self
    on:touchmove|preventDefault|self
    role="presentation"
  >
    <div class="settings-modal" bind:this={modalElement} role="dialog" aria-modal="true" aria-labelledby="settings-modal-title">
      <!-- Header bar with title and close button -->
      <div class="settings-header-bar">
        <h2 id="settings-modal-title" class="settings-header-title">{$t('settings.title')}</h2>
        <button class="modal-close-button" on:click={attemptClose} aria-label={$t('settings.modal.closeSettings')} title={$t('settings.modal.closeSettingsTitle')} style="position:static; margin:0; padding:0.5rem;">
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>

      <div class="settings-modal-container">
        <!-- Desktop sidebar -->
        <aside class="settings-sidebar">
          {#each sidebarSections as section}
            <div class="sidebar-section">
              <h3 class="section-heading">{section.title}</h3>
              <nav class="section-nav">
                {#each section.items as item}
                  <button
                    class="nav-item"
                    class:active={activeSection === item.id}
                    class:dirty={$settingsModalStore.dirtyState[item.id]}
                    on:click={() => switchSection(item.id)}
                  >
                    <span class="nav-item-label">{item.label}</span>
                    {#if $settingsModalStore.dirtyState[item.id]}
                      <span class="dirty-indicator" title={$t('settings.unsavedChanges')}>●</span>
                    {/if}
                  </button>
                {/each}
              </nav>
            </div>
          {/each}
        </aside>

        <!-- Content Area -->
        <main class="settings-content">
          <!-- Mobile section navigation (hidden on desktop where sidebar is visible) -->
          <div class="mobile-section-nav">
            <!-- svelte-ignore a11y_label_has_associated_control -->
            <label class="mobile-nav-label">{$t('settings.title')}</label>
            <select
              class="mobile-nav-select"
              value={activeSection}
              on:change={(e) => switchSection(e.currentTarget.value as SettingsSection)}
            >
              {#each sidebarSections as section}
                <optgroup label={section.title}>
                  {#each section.items as item}
                    <option value={item.id}>{item.label}</option>
                  {/each}
                </optgroup>
              {/each}
            </select>
          </div>

          <!-- Profile Section -->
          {#if activeSection === 'profile'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.profile.title')}</h3>
              <p class="section-description">{$t('settings.profile.description')}</p>

              <div class="profile-grid">
                <!-- Left Column: Profile Info + Language -->
                <div class="profile-card">
                  <h4 class="card-title">{$t('settings.profile.accountInfo')}</h4>
                  <form on:submit|preventDefault={updateProfile} class="settings-form">
                    <div class="form-group">
                      <label for="email">{$t('auth.email')}</label>
                      <input
                        type="email"
                        id="email"
                        class="form-control"
                        value={email}
                        disabled
                      />
                      <small class="form-text">{$t('settings.profile.emailCannotChange')}</small>
                    </div>

                    <div class="form-group">
                      <label for="fullName">{$t('settings.profile.fullName')}</label>
                      <input
                        type="text"
                        id="fullName"
                        class="form-control"
                        bind:value={fullName}
                        required
                      />
                    </div>

                    <div class="form-actions">
                      <button
                        type="submit"
                        class="btn btn-primary"
                        disabled={!profileChanged || profileLoading}
                      >
                        {profileLoading ? $t('common.saving') : $t('common.saveChanges')}
                      </button>
                    </div>
                  </form>

                  <div class="card-divider"></div>
                  <LanguageSettings />
                </div>

                <!-- Right Column: Password Change -->
                {#if isLocalUser}
                <div class="profile-card">
                  <h4 class="card-title">{$t('settings.profile.changePassword')}</h4>
                  <form on:submit|preventDefault={updatePassword} class="settings-form">
                    <div class="form-group">
                      <div class="password-header">
                        <label for="currentPassword">{$t('settings.profile.currentPassword')}</label>
                        <button
                          type="button"
                          class="toggle-password"
                          on:click={() => showCurrentPassword = !showCurrentPassword}
                          tabindex="-1"
                          aria-label={showCurrentPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
                        >
                          {#if showCurrentPassword}
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                              <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/>
                              <circle cx="12" cy="12" r="3"/>
                            </svg>
                          {:else}
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                              <path d="m15 18-.722-3.25"/>
                              <path d="m2 2 20 20"/>
                              <path d="m9 9-.637 3.181"/>
                              <path d="M12.5 5.5c2.13.13 4.16 1.11 5.5 3.5-.274.526-.568 1.016-.891 1.469"/>
                              <path d="M2 12s3-7 10-7c1.284 0 2.499.23 3.62.67"/>
                              <path d="m18.147 8.476.853 3.524"/>
                            </svg>
                          {/if}
                        </button>
                      </div>
                      <input
                        type={showCurrentPassword ? 'text' : 'password'}
                        id="currentPassword"
                        class="form-control"
                        bind:value={currentPassword}
                      />
                    </div>

                    <div class="form-group">
                      <div class="password-header">
                        <label for="newPassword">{$t('settings.profile.newPassword')}</label>
                        <button
                          type="button"
                          class="toggle-password"
                          on:click={() => showNewPassword = !showNewPassword}
                          tabindex="-1"
                          aria-label={showNewPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
                        >
                          {#if showNewPassword}
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                              <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/>
                              <circle cx="12" cy="12" r="3"/>
                            </svg>
                          {:else}
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                              <path d="m15 18-.722-3.25"/>
                              <path d="m2 2 20 20"/>
                              <path d="m9 9-.637 3.181"/>
                              <path d="M12.5 5.5c2.13.13 4.16 1.11 5.5 3.5-.274.526-.568 1.016-.891 1.469"/>
                              <path d="M2 12s3-7 10-7c1.284 0 2.499.23 3.62.67"/>
                              <path d="m18.147 8.476.853 3.524"/>
                            </svg>
                          {/if}
                        </button>
                      </div>
                      <input
                        type={showNewPassword ? 'text' : 'password'}
                        id="newPassword"
                        class="form-control"
                        bind:value={newPassword}
                      />
                      <small class="form-text">{$t('auth.passwordMinLength')}</small>
                    </div>

                    <div class="form-group">
                      <div class="password-header">
                        <label for="confirmPassword">{$t('settings.profile.confirmNewPassword')}</label>
                        <button
                          type="button"
                          class="toggle-password"
                          on:click={() => showConfirmPassword = !showConfirmPassword}
                          tabindex="-1"
                          aria-label={showConfirmPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
                        >
                          {#if showConfirmPassword}
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                              <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/>
                              <circle cx="12" cy="12" r="3"/>
                            </svg>
                          {:else}
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                              <path d="m15 18-.722-3.25"/>
                              <path d="m2 2 20 20"/>
                              <path d="m9 9-.637 3.181"/>
                              <path d="M12.5 5.5c2.13.13 4.16 1.11 5.5 3.5-.274.526-.568 1.016-.891 1.469"/>
                              <path d="M2 12s3-7 10-7c1.284 0 2.499.23 3.62.67"/>
                              <path d="m18.147 8.476.853 3.524"/>
                            </svg>
                          {/if}
                        </button>
                      </div>
                      <input
                        type={showConfirmPassword ? 'text' : 'password'}
                        id="confirmPassword"
                        class="form-control"
                        bind:value={confirmPassword}
                      />
                    </div>

                    <div class="form-actions">
                      <button
                        type="submit"
                        class="btn btn-primary"
                        disabled={!passwordChanged || passwordLoading}
                      >
                        {passwordLoading ? $t('common.updating') : $t('settings.profile.updatePassword')}
                      </button>
                    </div>
                  </form>
                </div>
                {/if}
              </div>

              <!-- Security / MFA Section — full width below -->
              <div class="mfa-card">
                <SecuritySettings />
              </div>
            </div>
          {/if}

          <!-- Recording Settings Section -->
          {#if activeSection === 'recording'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.recording.title')}</h3>
              <p class="section-description">{$t('settings.recording.description')}</p>

              <form on:submit|preventDefault={saveRecordingSettings} class="settings-form">
                <div class="form-group">
                  <label for="maxRecordingDuration">{$t('settings.recording.maxDuration')}</label>
                  <input
                    type="number"
                    id="maxRecordingDuration"
                    class="form-control"
                    bind:value={maxRecordingDuration}
                    on:input={handleRecordingSettingsChange}
                    min="15"
                    max="480"
                    required
                  />
                  <small class="form-text">{$t('settings.recording.durationRange')}</small>
                </div>

                <div class="form-group">
                  <label for="recordingQuality">{$t('settings.recording.quality')}</label>
                  <select
                    id="recordingQuality"
                    class="form-control"
                    bind:value={recordingQuality}
                    on:change={handleRecordingSettingsChange}
                  >
                    <option value="standard">{$t('settings.recording.qualityStandard')}</option>
                    <option value="high">{$t('settings.recording.qualityHigh')}</option>
                    <option value="maximum">{$t('settings.recording.qualityMaximum')}</option>
                  </select>
                  <small class="form-text">{$t('settings.recording.qualityHelp')}</small>
                </div>

                <div class="form-group">
                  <label class="checkbox-label">
                    <input
                      type="checkbox"
                      bind:checked={autoStopEnabled}
                      on:change={handleRecordingSettingsChange}
                    />
                    <span>{$t('settings.recording.autoStop')}</span>
                  </label>
                  <small class="form-text">{$t('settings.recording.autoStopHelp')}</small>
                </div>

                <div class="form-actions">
                  <button
                    type="button"
                    class="btn btn-secondary"
                    on:click={resetRecordingSettings}
                    disabled={recordingSettingsLoading}
                  >
                    {$t('common.resetToDefaults')}
                  </button>

                  <button
                    type="submit"
                    class="btn btn-primary"
                    disabled={!recordingSettingsChanged || recordingSettingsLoading}
                  >
                    {recordingSettingsLoading ? $t('common.saving') : $t('common.saveSettings')}
                  </button>
                </div>
              </form>
            </div>
          {/if}

          <!-- Audio Extraction Settings Section -->
          {#if activeSection === 'audio-extraction'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.audioExtraction.title')}</h3>
              <p class="section-description">{$t('settings.audioExtraction.description')}</p>
              <AudioExtractionSettings />
            </div>
          {/if}

          <!-- Transcription Settings Section -->
          {#if activeSection === 'transcription'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.transcription.title')}</h3>
              <p class="section-description">{$t('settings.transcription.description')}</p>
              <TranscriptionSettings />
            </div>
          {/if}

          <!-- Organization Context Section -->
          {#if activeSection === 'organization-context'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.orgContext.title')}</h3>
              <p class="section-description">{$t('settings.orgContext.description')}</p>
              <OrganizationContextSettings />
            </div>
          {/if}

          <!-- Speaker Attribute Settings Section -->
          {#if activeSection === 'speaker-attributes'}
            <div class="content-section">
              <SpeakerAttributeSettings />
            </div>
          {/if}

          <!-- Download Settings Section -->
          {#if activeSection === 'download'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.download.title')}</h3>
              <p class="section-description">{$t('settings.download.description')}</p>
              <DownloadSettings />
            </div>
          {/if}

          <!-- Media Sources Settings Section -->
          {#if activeSection === 'media-sources'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.mediaSources.title')}</h3>
              <p class="section-description">{$t('settings.mediaSources.description')}</p>
              <MediaSourcesSettings />
            </div>
          {/if}

          <!-- AI Prompts Section -->
          {#if activeSection === 'ai-prompts'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.aiPrompts.title')}</h3>
              <p class="section-description">{$t('settings.aiPrompts.description')}</p>
              <PromptSettings onSettingsChange={onAISettingsChange} />
            </div>
          {/if}

          <!-- LLM Provider Section -->
          {#if activeSection === 'llm-provider'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.llmProvider.title')}</h3>
              <p class="section-description">{$t('settings.llmProvider.description')}</p>
              <LLMSettings onSettingsChange={onAISettingsChange} {isAdmin} />
            </div>
          {/if}

          <!-- Auto-Labeling Section -->
          {#if activeSection === 'auto-labeling'}
            <div class="content-section">
              <h3 class="section-title">{$t('autoLabel.title')}</h3>
              <p class="section-description">{$t('autoLabel.description')}</p>
              <AutoLabelSettings />
            </div>
          {/if}

          <!-- ASR Provider Section -->
          {#if activeSection === 'asr-provider'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.asrProvider.sectionTitle')}</h3>
              <p class="section-description">{$t('settings.asrProvider.description')}</p>
              <ASRSettings {isAdmin} />
            </div>
          {/if}

          <!-- Engine Configuration Section (admin only) -->
          {#if activeSection === 'engine-settings' && isAdmin}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.engineSettings.title')}</h3>
              <p class="section-description">{$t('settings.engineSettings.description')}</p>
              <EngineSettings />
            </div>
          {/if}

          <!-- Custom Vocabulary Section -->
          {#if activeSection === 'custom-vocabulary'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.customVocabulary.title')}</h3>
              <p class="section-description">{$t('settings.customVocabulary.description')}</p>
              <CustomVocabularySettings />
            </div>
          {/if}

          <!-- Search & Indexing Section -->
          {#if activeSection === 'search-indexing'}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.searchIndexing.title')}</h3>
              <p class="section-description">{$t('settings.searchIndexing.description')}</p>
              <SearchSettings />
            </div>
          {/if}

          <!-- Groups Section -->
          {#if activeSection === 'groups'}
            <div class="content-section">
              <GroupsSettings />
            </div>
          {/if}

          <!-- Admin Users Section -->
          {#if activeSection === 'admin-users' && isAdmin}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.users.title')}</h3>
              <p class="section-description">{$t('settings.users.description')}</p>
              {#if isSuperAdmin}
                <AccountStatusDashboard />
              {/if}
              <UserManagementTable
                {users}
                loading={usersLoading}
                onRefresh={refreshAdminUsers}
                onUserRecovery={recoverUserFiles}
              />
            </div>
          {/if}

          <!-- System Statistics Section -->
          {#if activeSection === 'system-statistics'}
            <div class="content-section">
              <div class="section-header-row">
                <div>
                  <h3 class="section-title">{$t('settings.statistics.title')}</h3>
                  <p class="section-description">{$t('settings.statistics.description')}</p>
                </div>
                <button
                  type="button"
                  class="btn btn-secondary btn-refresh"
                  on:click={refreshStats}
                  disabled={statsLoading || statsRefreshing}
                >
                  <svg class="refresh-icon" class:spinning={statsRefreshing} xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
                  </svg>
                  {$t('settings.statistics.refresh')}
                </button>
              </div>

              {#if statsLoading}
                <div class="loading-state">
                  <Spinner size="large" />
                  <p>{$t('settings.statistics.loadingMessage')}</p>
                </div>
              {:else}
                <div class="stats-grid" class:stats-refreshing={statsRefreshing}>
                  <!-- User Stats -->
                  <div class="stat-card">
                    <h4>{$t('settings.statistics.users')}</h4>
                    <div class="stat-value">{stats.users?.total || 0}</div>
                    <div class="stat-detail">{$t('settings.statistics.newUsers')}: {stats.users?.new || 0}</div>
                  </div>

                  <!-- Media Stats -->
                  <div class="stat-card">
                    <h4>{$t('settings.statistics.mediaFiles')}</h4>
                    <div class="stat-value">{stats.files?.total || 0}</div>
                    <div class="stat-detail">{$t('settings.statistics.new')}: {stats.files?.new || 0}</div>
                    <div class="stat-detail">{$t('settings.statistics.segments')}: {stats.files?.segments || 0}</div>
                  </div>

                  <!-- Task Stats -->
                  <div class="stat-card">
                    <h4>{$t('settings.statistics.tasks')}</h4>
                    <div class="stat-detail">{$t('settings.statistics.pending')}: {stats.tasks?.pending || 0}</div>
                    <div class="stat-detail">{$t('settings.statistics.running')}: {stats.tasks?.running || 0}</div>
                    <div class="stat-detail">{$t('settings.statistics.completed')}: {stats.tasks?.completed || 0}</div>
                    <div class="stat-detail">{$t('settings.statistics.failed')}: {stats.tasks?.failed || 0}</div>
                    <div class="stat-detail">{$t('settings.statistics.successRate')}: {stats.tasks?.success_rate || 0}%</div>
                  </div>

                  <!-- Performance Stats -->
                  <!-- svelte-ignore a11y-click-events-have-key-events -->
                  <!-- svelte-ignore a11y-no-static-element-interactions -->
                  <div class="stat-card stat-card-clickable" on:click={() => openProcessingDetails('performance')}>
                    <h4>{$t('settings.statistics.performance')}</h4>
                    <div class="stat-detail">{$t('settings.statistics.avgProcessTime')}: {formatTime(stats.tasks?.avg_processing_time || 0)}</div>
                    <div class="stat-detail">{$t('settings.statistics.fileTimingAvg')}: {formatTime(stats.file_timing?.avg_secs || 0)}</div>
                    <div class="stat-detail">{$t('settings.statistics.fileTimingMin')}: {formatTime(stats.file_timing?.min_secs || 0)}</div>
                    <div class="stat-detail">{$t('settings.statistics.fileTimingMax')}: {formatTime(stats.file_timing?.max_secs || 0)}</div>
                    <div class="stat-detail">{$t('settings.statistics.speakers')}: {stats.speakers?.total || 0}</div>
                    <div class="stat-detail stat-detail-hint">{$t('settings.statistics.viewDetails')}</div>
                  </div>

                  <!-- Throughput & ETA -->
                  <!-- svelte-ignore a11y-click-events-have-key-events -->
                  <!-- svelte-ignore a11y-no-static-element-interactions -->
                  <div class="stat-card stat-card-clickable" on:click={() => openProcessingDetails('throughput')}>
                    <h4>{$t('settings.statistics.throughput')}</h4>
                    <div class="stat-value">{stats.throughput?.rate_1h || 0} <span class="stat-unit">{$t('settings.statistics.filesPerHour')}</span></div>
                    <div class="stat-detail">{$t('settings.statistics.avgRate3h')}: {stats.throughput?.rate_3h || 0} {$t('settings.statistics.filesPerHour')}</div>
                    {#if stats.eta?.remaining > 0}
                      <div class="stat-detail">{$t('settings.statistics.remaining')}: {stats.eta.remaining} files</div>
                      {#if stats.eta.hours_remaining !== null}
                        <div class="stat-detail">{$t('settings.statistics.hoursRemaining')}: {stats.eta.hours_remaining}h</div>
                      {/if}
                    {:else}
                      <div class="stat-detail">{$t('settings.statistics.noActiveProcessing')}</div>
                    {/if}
                    <div class="stat-detail stat-detail-hint">{$t('settings.statistics.viewDetails')}</div>
                  </div>

                  <!-- Queue Depths -->
                  <!-- svelte-ignore a11y-click-events-have-key-events -->
                  <!-- svelte-ignore a11y-no-static-element-interactions -->
                  <div class="stat-card stat-card-clickable" on:click={() => openProcessingDetails('queues')}>
                    <h4>{$t('settings.statistics.queueDepths')}</h4>
                    <div class="stat-value">{stats.queues?.total || 0} <span class="stat-unit">{$t('settings.statistics.queueTotal')}</span></div>
                    {#if stats.queues?.total > 0}
                      <div class="queue-bars">
                        {#each [
                          { key: 'gpu', label: $t('settings.statistics.queueGpu') },
                          { key: 'download', label: $t('settings.statistics.queueDownload') },
                          { key: 'nlp', label: $t('settings.statistics.queueNlp') },
                          { key: 'embedding', label: $t('settings.statistics.queueEmbedding') },
                          { key: 'cpu', label: $t('settings.statistics.queueCpu') }
                        ] as queue}
                          {#if stats.queues?.[queue.key] > 0}
                            <div class="stat-detail">{queue.label}: {stats.queues[queue.key]}</div>
                          {/if}
                        {/each}
                      </div>
                    {/if}
                    <div class="stat-detail stat-detail-hint">{$t('settings.statistics.viewDetails')}</div>
                  </div>

                  <!-- AI Models -->
                  <!-- svelte-ignore a11y-click-events-have-key-events -->
                  <!-- svelte-ignore a11y-no-static-element-interactions -->
                  <div class="stat-card model-card stat-card-clickable" on:click={() => openProcessingDetails('models')}>
                    <h4>{$t('settings.statistics.aiModels')}</h4>
                    {#if stats.models}
                      <div class="model-info">
                        <div class="model-item">
                          <span class="model-label">{$t('settings.statistics.whisperModel')}:</span>
                          <span class="model-value">{stats.models.whisper?.name || 'N/A'}</span>
                        </div>
                        <div class="model-item">
                          <span class="model-label">{$t('settings.statistics.diarization')}:</span>
                          <span class="model-value">{stats.models.diarization?.name || 'N/A'}</span>
                        </div>
                        {#if stats.models.search_embedding}
                          <div class="model-item">
                            <span class="model-label">{$t('settings.statistics.searchModel')}:</span>
                            <span class="model-value">{stats.models.search_embedding.name}</span>
                          </div>
                        {/if}
                        {#if stats.models.llm}
                          <div class="model-item">
                            <span class="model-label">{$t('settings.statistics.llmModel')}:</span>
                            <span class="model-value">{stats.models.llm.name}</span>
                          </div>
                        {/if}
                      </div>
                    {:else}
                      <div class="stat-detail">{$t('settings.statistics.modelNotAvailable')}</div>
                    {/if}
                    <div class="stat-detail stat-detail-hint">{$t('settings.statistics.viewDetails')}</div>
                  </div>

                  <!-- Search Index Status -->
                  {#if searchIndexStatus}
                    <div class="stat-card">
                      <h4>{$t('settings.statistics.searchIndex')}</h4>
                      <div class="stat-value">
                        {searchIndexStatus.indexed_files}/{searchIndexStatus.total_files}
                        <span class="stat-unit">{$t('settings.statistics.indexed')}</span>
                      </div>
                      <div class="stat-detail">{$t('settings.statistics.model')}: {searchIndexStatus.current_model}</div>
                      {#if searchIndexStatus.pending_files > 0}
                        <div class="stat-detail stat-detail-warning">{searchIndexStatus.pending_files} {$t('settings.statistics.pendingReindex')}</div>
                      {/if}
                      {#if searchIndexStatus.in_progress}
                        <div class="stat-detail stat-detail-active">{$t('settings.statistics.reindexingActive')}</div>
                      {/if}
                      {#if searchHealthStatus}
                        <div class="search-health-row">
                          {#each Object.entries(searchHealthStatus) as [name, info]}
                            <span class="search-health-dot" class:healthy={info.status === 'green'} class:error={info.status === 'red'} title="{name}: {info.status === 'green' ? $t('settings.search.indexGreen') : $t('settings.search.indexRed')}"></span>
                          {/each}
                          <span class="search-health-label">
                            {#if Object.values(searchHealthStatus).every(i => i.status === 'green')}
                              {$t('settings.statistics.allHealthy')}
                            {:else}
                              {$t('settings.statistics.needsRepair')}
                            {/if}
                          </span>
                        </div>
                      {/if}
                    </div>
                  {/if}

                  <!-- System Resources: CPU & Memory -->
                  <div class="stat-card stat-card-stacked">
                    <div class="stat-section">
                      <h4>{$t('settings.statistics.cpuUsage')}</h4>
                      <div class="stat-value">{stats.system?.cpu?.total_percent || '0%'}</div>
                      <div class="progress-bar">
                        <div class="progress-fill" style="width: {parseFloat(stats.system?.cpu?.total_percent) || 0}%"></div>
                      </div>
                    </div>

                    <div class="stat-section">
                      <h4>{$t('settings.statistics.memoryUsage')}</h4>
                      <div class="stat-value">{stats.system?.memory?.percent || '0%'}</div>
                      <div class="stat-detail-compact">
                        {stats.system?.memory?.used || $t('common.unknown')} / {stats.system?.memory?.total || $t('common.unknown')}
                      </div>
                      <div class="progress-bar">
                        <div class="progress-fill" style="width: {parseFloat(stats.system?.memory?.percent) || 0}%"></div>
                      </div>
                    </div>
                  </div>

                  <div class="stat-card stat-card-with-bar">
                    <div class="stat-card-content">
                      <h4>{$t('settings.statistics.diskUsage')}</h4>
                      <div class="stat-value">{stats.system?.disk?.percent || '0%'}</div>
                      <div class="stat-detail">
                        <span>{$t('settings.statistics.total')}: {stats.system?.disk?.total || $t('common.unknown')}</span>
                        <span>{$t('settings.statistics.used')}: {stats.system?.disk?.used || $t('common.unknown')}</span>
                        <span>{$t('settings.statistics.free')}: {stats.system?.disk?.free || $t('common.unknown')}</span>
                      </div>
                    </div>
                    <div class="progress-bar">
                      <div class="progress-fill" style="width: {parseFloat(stats.system?.disk?.percent) || 0}%"></div>
                    </div>
                  </div>

                  <!-- GPU VRAM -->
                  <div class="stat-card stat-card-with-bar">
                    {#if activeGpu?.available}
                      <div class="stat-card-content">
                        <h4 class="gpu-card-header">
                          <span>{$t('settings.statistics.gpuVram')}</span>
                          {#if gpuCount > 1}
                            <div class="gpu-stepper">
                              <button class="gpu-step-btn" on:click={() => currentGpuIndex = (currentGpuIndex - 1 + gpuCount) % gpuCount} aria-label={$t('settings.statistics.previousGpu')}>&#8249;</button>
                              <span class="gpu-step-label">GPU {currentGpuIndex + 1}/{gpuCount}</span>
                              <button class="gpu-step-btn" on:click={() => currentGpuIndex = (currentGpuIndex + 1) % gpuCount} aria-label={$t('settings.statistics.nextGpu')}>&#8250;</button>
                            </div>
                          {/if}
                        </h4>
                        <div class="stat-value">{activeGpu.memory_percent || '0%'}</div>
                        <div class="stat-detail">
                          <span>{$t('settings.statistics.gpu')}: {activeGpu.name || $t('common.unknown')}</span>
                          <span>{$t('settings.statistics.total')}: {activeGpu.memory_total || $t('common.unknown')}</span>
                          <span>{$t('settings.statistics.used')}: {activeGpu.memory_used || $t('common.unknown')}</span>
                          <span>{$t('settings.statistics.free')}: {activeGpu.memory_free || $t('common.unknown')}</span>
                          {#if activeGpu.utilization_percent && activeGpu.utilization_percent !== 'N/A'}
                            <span>{$t('settings.statistics.gpuUtilization')}: {activeGpu.utilization_percent}</span>
                          {/if}
                          {#if activeGpu.temperature_celsius !== null && activeGpu.temperature_celsius !== undefined}
                            <span>{$t('settings.statistics.gpuTemperature')}: {activeGpu.temperature_celsius}°C</span>
                          {/if}
                        </div>
                      </div>
                      <div class="progress-bar">
                        <div class="progress-fill" style="width: {parseFloat(activeGpu.memory_percent) || 0}%"></div>
                      </div>
                    {:else if activeGpu?.loading}
                      <div class="stat-card-content">
                        <h4>{$t('settings.statistics.gpuVram')}</h4>
                        <div class="stat-value loading-text">{$t('common.loading')}</div>
                        <div class="stat-detail">{$t('settings.statistics.gpuStatsLoading') || 'Collecting GPU stats from worker...'}</div>
                      </div>
                    {:else}
                      <div class="stat-card-content">
                        <h4>{$t('settings.statistics.gpuVram')}</h4>
                        <div class="stat-value">N/A</div>
                        <div class="stat-detail">{activeGpu?.name || $t('settings.statistics.noGpu')}</div>
                      </div>
                    {/if}
                  </div>
                </div>

                <!-- Recent Tasks Table -->
                {#if stats.tasks?.recent && stats.tasks.recent.length > 0}
                  <div class="recent-tasks" class:stats-refreshing={statsRefreshing}>
                    <h4>{$t('settings.statistics.recentTasks')}</h4>
                    <div class="table-container">
                      <table class="data-table">
                        <thead>
                          <tr>
                            <th>{$t('settings.statistics.taskId')}</th>
                            <th>{$t('settings.statistics.type')}</th>
                            <th>{$t('settings.statistics.status')}</th>
                            <th>{$t('settings.statistics.created')}</th>
                            <th>{$t('settings.statistics.elapsed')}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {#each stats.tasks.recent as task}
                            <tr>
                              <td>{task.id.substring(0, 8)}...</td>
                              <td>{task.type}</td>
                              <td>
                                <span class="status-badge status-{task.status}">{formatStatus(task.status)}</span>
                              </td>
                              <td>{new Date(task.created_at).toLocaleString()}</td>
                              <td>{formatTime(task.elapsed)}</td>
                            </tr>
                          {/each}
                        </tbody>
                      </table>
                    </div>
                  </div>
                {:else}
                  <div class="recent-tasks" class:stats-refreshing={statsRefreshing}>
                    <h4>{$t('settings.statistics.recentTasks')}</h4>
                    <p class="empty-state">{$t('settings.statistics.noRecentTasks')}</p>
                  </div>
                {/if}
              {/if}
            </div>
          {/if}

          <!-- Admin Task Health Section -->
          {#if activeSection === 'admin-task-health' && isAdmin}
            <div class="content-section">
              <div class="section-header-row">
                <div>
                  <h3 class="section-title">{$t('settings.taskHealth.title')}</h3>
                  <p class="section-description">{$t('settings.taskHealth.description')}</p>
                </div>
                <button
                  type="button"
                  class="btn btn-secondary btn-refresh"
                  on:click={refreshTaskHealth}
                  disabled={taskHealthLoading}
                >
                  <svg class="refresh-icon" class:spinning={taskHealthLoading} xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
                  </svg>
                  {taskHealthLoading ? $t('settings.taskHealth.loading') : $t('settings.taskHealth.refresh')}
                </button>
              </div>

              <!-- Retry Settings (task retry configuration) -->
              <div class="settings-subsection">
                <RetrySettings />
              </div>

              {#if taskHealthLoading}
                <div class="loading-state">
                  <Spinner size="large" />
                  <p>{$t('settings.taskHealth.loadingMessage')}</p>
                </div>
              {:else if taskHealthData}
                <div class="task-health-grid">
                  <!-- Recovery Actions -->
                  <div class="health-card">
                    <h4>{$t('settings.taskHealth.systemRecovery')}</h4>
                    <div class="action-buttons">
                      <button class="btn btn-warning" on:click={recoverStuckTasks}>
                        {$t('settings.taskHealth.recoverStuck')} ({taskHealthData.stuck_tasks?.length || 0})
                      </button>
                      <button class="btn btn-warning" on:click={fixInconsistentFiles}>
                        {$t('settings.taskHealth.fixInconsistent')} ({taskHealthData.inconsistent_files?.length || 0})
                      </button>
                      <button class="btn btn-primary" on:click={startupRecovery}>
                        {$t('settings.taskHealth.startupRecovery')}
                      </button>
                      <button class="btn btn-primary" on:click={recoverAllUserFiles}>
                        {$t('settings.taskHealth.recoverAllUsers')}
                      </button>
                    </div>
                  </div>

                  <!-- Stuck Tasks -->
                  {#if taskHealthData.stuck_tasks && taskHealthData.stuck_tasks.length > 0}
                    <div class="health-card">
                      <h4>{$t('settings.taskHealth.stuckTasks')}</h4>
                      <div class="table-container">
                        <table class="data-table">
                          <thead>
                            <tr>
                              <th>{$t('settings.taskHealth.id')}</th>
                              <th>{$t('settings.statistics.type')}</th>
                              <th>{$t('settings.statistics.status')}</th>
                              <th>{$t('settings.statistics.created')}</th>
                              <th>{$t('settings.taskHealth.actions')}</th>
                            </tr>
                          </thead>
                          <tbody>
                            {#each taskHealthData.stuck_tasks as task}
                              <tr>
                                <td>{task.id}</td>
                                <td>{task.task_type}</td>
                                <td><span class="status-badge status-{task.status}">{formatStatus(task.status)}</span></td>
                                <td>{new Date(task.created_at).toLocaleString()}</td>
                                <td>
                                  <button class="btn-small btn-primary" on:click={() => retryTask(task.id)}>
                                    {$t('settings.taskHealth.retry')}
                                  </button>
                                </td>
                              </tr>
                            {/each}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  {/if}

                  <!-- Inconsistent Files -->
                  {#if taskHealthData.inconsistent_files && taskHealthData.inconsistent_files.length > 0}
                    <div class="health-card">
                      <h4>{$t('settings.taskHealth.inconsistentFiles')}</h4>
                      <div class="table-container">
                        <table class="data-table">
                          <thead>
                            <tr>
                              <th>{$t('settings.taskHealth.id')}</th>
                              <th>{$t('settings.taskHealth.filename')}</th>
                              <th>{$t('settings.statistics.status')}</th>
                              <th>{$t('settings.taskHealth.actions')}</th>
                            </tr>
                          </thead>
                          <tbody>
                            {#each taskHealthData.inconsistent_files as file}
                              <tr>
                                <td>{file.uuid}</td>
                                <td>{file.filename}</td>
                                <td><span class="status-badge status-{file.status}">{formatStatus(file.status)}</span></td>
                                <td>
                                  <button class="btn-small btn-primary" on:click={() => retryFile(file.uuid)}>
                                    {$t('settings.taskHealth.retry')}
                                  </button>
                                </td>
                              </tr>
                            {/each}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  {/if}
                </div>
              {:else}
                <div class="placeholder-message">
                  <p>{$t('settings.taskHealth.clickRefresh')}</p>
                </div>
              {/if}
            </div>
          {/if}

          <!-- Admin System Settings: removed (retry config moved to task health) -->

          <!-- Embedding Migration Section -->
          {#if activeSection === 'embedding-migration' && isAdmin}
            <div class="content-section">
              <EmbeddingMigrationSettings />
              <EmbeddingConsistencySettings />
            </div>
          {/if}

          <!-- Data Integrity Section -->
          {#if activeSection === 'data-integrity' && isAdmin}
            <div class="content-section">
              <DataIntegritySettings />
            </div>
          {/if}

          <!-- File Retention Section -->
          {#if activeSection === 'retention' && isAdmin}
            <div class="content-section">
              <RetentionSettings />
            </div>
          {/if}

          <!-- Authentication Settings Section (Super Admin only) -->
          {#if activeSection === 'authentication' && isSuperAdmin}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.authentication.title')}</h3>
              <p class="section-description">{$t('settings.authentication.description')}</p>
              <AuthenticationSettings />
            </div>
          {/if}


          <!-- Audit Log Viewer (Super Admin only) -->
          {#if activeSection === 'audit-logs' && isSuperAdmin}
            <div class="content-section">
              <h3 class="section-title">{$t('settings.auditLog.sectionTitle')}</h3>
              <p class="section-description">{$t('settings.auditLog.sectionDescription')}</p>
              <AuditLogViewer />
            </div>
          {/if}
        </main>
      </div>
    </div>
  </div>
{/if}

<!-- Close Confirmation Modal -->
<ConfirmationModal
  bind:isOpen={showCloseConfirmation}
  title={$t('settings.unsavedChanges')}
  message={$t('settings.unsavedChangesMessage')}
  confirmText={$t('settings.closeWithoutSaving')}
  cancelText={$t('settings.keepEditing')}
  confirmButtonClass="btn-danger"
  cancelButtonClass="btn-secondary"
  on:confirm={forceClose}
  on:cancel={() => showCloseConfirmation = false}
  on:close={() => showCloseConfirmation = false}
/>

<!-- Admin Confirmation Modal -->
<ConfirmationModal
  bind:isOpen={showConfirmModal}
  title={confirmModalTitle}
  message={confirmModalMessage}
  confirmText={$t('settings.confirm')}
  cancelText={$t('settings.cancel')}
  confirmButtonClass="btn-primary"
  cancelButtonClass="btn-secondary"
  on:confirm={handleConfirmModalConfirm}
  on:cancel={handleConfirmModalCancel}
  on:close={handleConfirmModalCancel}
/>

<!-- Processing Details Modal -->
<ProcessingDetailsModal
  bind:isOpen={showProcessingDetails}
  bind:section={processingDetailsSection}
  {stats}
/>

<style>
  .settings-modal-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background-color: var(--modal-backdrop);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1300;
    animation: fadeIn 0.2s ease-out;
    overflow: hidden;
    overscroll-behavior: none;
  }

  @keyframes fadeIn {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }

  .settings-modal {
    position: relative;
    width: 90vw;
    max-width: 1200px;
    height: 85vh;
    max-height: 900px;
    background-color: var(--surface-color);
    border-radius: 12px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
    overflow: hidden;
    animation: slideUp 0.3s ease-out;
    display: flex;
    flex-direction: column;
  }

  @keyframes slideUp {
    from {
      transform: translateY(20px);
      opacity: 0;
    }
    to {
      transform: scale(1);
      opacity: 1;
    }
  }

  .settings-header-bar {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
    min-height: 44px;
  }

  .settings-header-title {
    font-weight: 600;
    font-size: 1.1rem;
    color: var(--text-color);
    margin: 0;
    margin-right: auto;
  }

  .modal-close-button {
    position: absolute;
    top: 0.75rem;
    right: 0.75rem;
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.5rem;
    color: var(--text-secondary);
    transition: color 0.2s ease;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 10;
  }

  .modal-close-button:hover {
    color: var(--text-color);
    background: var(--button-hover, var(--background-color));
  }

  .settings-modal-container {
    display: flex;
    flex: 1;
    min-height: 0;
    overflow: hidden;
  }

  .settings-sidebar {
    width: 240px;
    background-color: var(--background-color);
    border-right: 1px solid var(--border-color);
    padding: 0.75rem 0;
    overflow-y: auto;
    flex-shrink: 0;
    overscroll-behavior: contain;
  }

  .sidebar-section {
    margin-bottom: 0.25rem;
    padding-top: 0.75rem;
    border-top: 1px solid var(--border-color);
  }

  .sidebar-section:first-child {
    border-top: none;
    padding-top: 0;
  }

  .section-heading {
    font-size: 0.6875rem;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-tertiary, var(--text-secondary));
    opacity: 0.7;
    margin: 0 1.25rem 0.375rem;
    padding-top: 0;
  }

  .section-nav {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .nav-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.4375rem 0.75rem;
    margin: 0 0.5rem;
    border: none;
    border-radius: 6px;
    background-color: transparent;
    color: var(--text-color);
    text-align: left;
    cursor: pointer;
    transition: background-color 0.15s, color 0.15s;
    font-size: 0.8125rem;
    position: relative;
  }

  .nav-item:hover {
    background-color: var(--hover-color, rgba(0, 0, 0, 0.04));
    color: var(--primary-color);
  }

  .nav-item.active {
    background-color: var(--primary-light);
    color: var(--primary-color);
    font-weight: 500;
  }

  .nav-item-label {
    flex: 1;
  }

  .dirty-indicator {
    color: var(--warning-color);
    font-size: 1.2em;
    line-height: 1;
  }

  .settings-content {
    flex: 1;
    overflow-y: auto;
    padding: 1.5rem;
    overscroll-behavior: contain;
  }

  /* Mobile section navigation - hidden when sidebar is visible */
  .mobile-section-nav {
    display: none;
    margin-bottom: 1rem;
    padding: 0.5rem;
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
  }

  .mobile-nav-label {
    display: block;
    font-size: 0.75rem;
    font-weight: 600;
    margin-bottom: 0.25rem;
    color: var(--text-secondary);
  }

  .mobile-nav-select {
    width: 100%;
    padding: 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--surface-color);
    color: var(--text-color);
    font-size: 1rem;
    min-height: 44px;
  }

  .content-section {
    max-width: 100%;
  }

  .section-title {
    font-size: 1.125rem;
    font-weight: 600;
    margin: 0 0 0.25rem 0;
    color: var(--text-color);
  }

  .section-description {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    margin: 0 0 1.25rem 0;
  }

  .password-section-divider {
    margin-top: 2rem;
    padding-top: 1.5rem;
    border-top: 1px solid var(--border-color);
  }

  .subsection-title {
    font-size: 0.9375rem;
    font-weight: 600;
    margin: 0 0 1rem 0;
    color: var(--text-color);
  }

  .profile-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
  }

  @media (max-width: 700px) {
    .profile-grid {
      grid-template-columns: 1fr;
    }
  }

  .profile-card {
    padding: 1rem;
    border-radius: 10px;
    background: var(--surface-color, #333);
    border: 1px solid var(--border-color, #444);
  }

  .card-title {
    font-size: 0.9rem;
    font-weight: 600;
    margin: 0 0 0.75rem 0;
    color: var(--text-color);
  }

  .card-divider {
    border-top: 1px solid var(--border-color, #444);
    margin: 1rem 0;
  }

  .mfa-card {
    margin-top: 1rem;
    padding: 1rem;
    border-radius: 10px;
    background: var(--surface-color, #333);
    border: 1px solid var(--border-color, #444);
  }

  .settings-form {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .form-group label {
    font-weight: 500;
    color: var(--text-color);
    font-size: 0.8125rem;
  }

  .form-control {
    padding: 0.5rem 0.625rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    transition: border-color 0.15s, box-shadow 0.15s;
  }

  .form-control:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px var(--primary-light);
  }

  .form-control:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    background-color: var(--background-color);
  }

  .form-text {
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin-top: 0.125rem;
  }

  .password-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .toggle-password {
    background: none;
    border: none;
    cursor: pointer;
    padding: 4px;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    border-radius: 4px;
    transition: background-color 0.2s;
  }

  .toggle-password:hover {
    background-color: var(--background-color);
    color: var(--text-color);
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    font-weight: normal;
    font-size: 0.8125rem;
  }

  .checkbox-label input[type="checkbox"] {
    width: 16px;
    height: 16px;
    cursor: pointer;
  }

  .form-actions {
    display: flex;
    gap: 0.75rem;
    margin-top: 0.75rem;
    justify-content: flex-end;
  }

  .form-actions .btn-secondary {
    margin-right: auto;
  }

  .btn {
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    border: none;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-primary {
    background-color: #3b82f6;
    color: white;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-primary:hover:not(:disabled) {
    background-color: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-primary:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-secondary {
    background-color: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
  }

  .btn-secondary:hover:not(:disabled) {
    background-color: var(--button-hover);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
  }

  .btn-secondary:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-secondary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-warning {
    background-color: var(--warning-color);
    color: white;
    box-shadow: 0 2px 4px rgba(245, 158, 11, 0.2);
  }

  .btn-warning:hover:not(:disabled) {
    background-color: #d97706;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(245, 158, 11, 0.25);
  }

  .btn-warning:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-danger {
    background-color: var(--error-color);
    color: white;
    box-shadow: 0 2px 4px rgba(239, 68, 68, 0.2);
  }

  .btn-danger:hover:not(:disabled) {
    background-color: #dc2626;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(239, 68, 68, 0.25);
  }

  .btn-danger:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-small {
    padding: 0.25rem 0.625rem;
    font-size: 0.75rem;
  }

  .btn-refresh {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
  }

  .refresh-icon {
    flex-shrink: 0;
    transition: transform 0.3s ease;
  }

  .refresh-icon.spinning {
    animation: spin 1s linear infinite;
  }

  .stats-refreshing {
    opacity: 0.45;
    pointer-events: none;
    transition: opacity 0.3s ease;
  }

  .section-header-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    margin-bottom: 1rem;
    padding-right: 2rem;
  }

  .section-header-row .section-description {
    margin-bottom: 0;
  }

  .loading-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 2rem;
    color: var(--text-secondary);
  }

  .loading-state p {
    margin: 0;
    font-size: 0.8125rem;
  }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 1rem;
    margin-bottom: 1.5rem;
  }

  .stat-card {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem;
  }

  .stat-card-clickable {
    cursor: pointer;
    transition: border-color 0.2s, box-shadow 0.2s;
  }

  .stat-card-clickable:hover {
    border-color: var(--primary-color);
    box-shadow: 0 2px 8px rgba(var(--primary-color-rgb), 0.1);
  }

  .stat-card-with-bar {
    display: flex;
    flex-direction: column;
  }

  .stat-card-stacked {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .stat-section {
    display: flex;
    flex-direction: column;
  }

  .stat-section h4 {
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
    margin: 0 0 0.5rem 0;
  }

  .stat-section .stat-value {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text-color);
    margin-bottom: 0.375rem;
  }

  .stat-detail-compact {
    font-size: 0.6875rem;
    color: var(--text-secondary);
    margin-bottom: 0.375rem;
  }

  .stat-card-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    margin-bottom: 0.75rem;
  }

  .stat-card h4 {
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
    margin: 0 0 0.5rem 0;
  }

  .stat-value {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text-color);
    margin-bottom: 0.375rem;
  }

  .loading-text {
    opacity: 0.6;
    animation: pulse 1.5s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 0.6; }
    50% { opacity: 1; }
  }

  .stat-detail {
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin-bottom: 0.125rem;
  }

  .stat-detail span {
    display: block;
    margin-bottom: 0.125rem;
  }

  .stat-detail-hint {
    margin-top: 0.5rem;
    font-size: 0.6875rem;
    opacity: 0.6;
    font-style: italic;
  }

  .stat-unit {
    font-size: 0.75rem;
    font-weight: 400;
    color: var(--text-secondary);
  }

  .stat-detail-warning {
    color: var(--warning-text, #92400e);
  }

  :global([data-theme='dark']) .stat-detail-warning {
    color: #fbbf24;
  }

  .stat-detail-active {
    color: var(--primary-color);
    font-weight: 500;
  }

  .search-health-row {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    margin-top: 0.375rem;
  }

  .search-health-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--text-secondary);
  }

  .search-health-dot.healthy {
    background: #22c55e;
  }

  .search-health-dot.error {
    background: #ef4444;
  }

  .search-health-label {
    font-size: 0.6875rem;
    color: var(--text-secondary);
  }

  .gpu-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .gpu-stepper {
    display: flex;
    align-items: center;
    gap: 0.2rem;
    margin-left: auto;
  }

  .gpu-step-btn {
    background: none;
    border: 1px solid var(--border-color);
    border-radius: 3px;
    color: var(--text-secondary);
    cursor: pointer;
    font-size: 0.875rem;
    line-height: 1;
    padding: 0 0.3rem;
    transition: background-color 0.15s, color 0.15s;
  }

  .gpu-step-btn:hover {
    background-color: var(--primary-light, var(--border-color));
    color: var(--primary-color);
    border-color: var(--primary-color);
  }

  .gpu-step-label {
    font-size: 0.6rem;
    font-weight: 600;
    color: var(--text-secondary);
    letter-spacing: 0.03em;
    white-space: nowrap;
  }

  .progress-bar {
    width: 100%;
    height: 8px;
    background-color: var(--border-color);
    border-radius: 4px;
    overflow: hidden;
    margin-top: 0;
  }

  .progress-fill {
    height: 100%;
    background-color: #3b82f6;
    transition: width 0.3s ease;
  }

  .recent-tasks {
    margin-top: 1.5rem;
  }

  .recent-tasks h4 {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-color);
    margin: 0 0 0.75rem 0;
  }

  .table-container {
    overflow-x: auto;
    border: 1px solid var(--border-color);
    border-radius: 8px;
  }

  .data-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .data-table thead {
    background-color: var(--background-color);
  }

  .data-table th {
    padding: 0.5rem 0.75rem;
    text-align: left;
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-color);
  }

  .data-table td {
    padding: 0.625rem 0.75rem;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .data-table tbody tr:last-child td {
    border-bottom: none;
  }

  .data-table tbody tr:hover {
    background-color: var(--background-color);
  }

  .status-badge {
    display: inline-block;
    padding: 0.125rem 0.5rem;
    border-radius: 10px;
    font-size: 0.6875rem;
    font-weight: 500;
    text-transform: capitalize;
  }

  .status-completed,
  .status-success {
    background-color: #d1fae5;
    color: #065f46;
  }

  .status-running,
  .status-processing,
  .status-in_progress {
    background-color: #dbeafe;
    color: #1e40af;
  }

  .status-pending {
    background-color: #fef3c7;
    color: #92400e;
  }

  .status-failed,
  .status-error {
    background-color: #fee2e2;
    color: #991b1b;
  }

  :global([data-theme='dark']) .status-completed,
  :global([data-theme='dark']) .status-success {
    background-color: #064e3b;
    color: #6ee7b7;
  }

  :global([data-theme='dark']) .status-running,
  :global([data-theme='dark']) .status-processing,
  :global([data-theme='dark']) .status-in_progress {
    background-color: #1e3a8a;
    color: #93c5fd;
  }

  :global([data-theme='dark']) .status-pending {
    background-color: #78350f;
    color: #fde68a;
  }

  :global([data-theme='dark']) .status-failed,
  :global([data-theme='dark']) .status-error {
    background-color: #7f1d1d;
    color: #fca5a5;
  }

  .task-health-grid {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .health-card {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem;
  }

  .health-card h4 {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-color);
    margin: 0 0 0.75rem 0;
  }

  .action-buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .placeholder-message {
    text-align: center;
    padding: 2rem;
    color: var(--text-secondary);
    font-size: 0.8125rem;
  }

  .settings-subsection {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1.25rem;
    margin-bottom: 1rem;
  }

  .empty-state {
    text-align: center;
    padding: 1rem;
    color: var(--text-secondary);
    font-size: 0.8125rem;
    font-style: italic;
  }

  /* AI Models Card Styles */
  .model-card {
    grid-column: span 1;
  }

  .model-info {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .model-item {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
  }

  .model-label {
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
  }

  .model-value {
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-color);
    font-family: 'Courier New', Courier, monospace;
  }

  /* Responsive Design */

  /* Raise above navbar (z-index 1200) on tablet so close button is reachable */
  @media (max-width: 768px) {
    .settings-modal {
      width: 100vw;
      height: 100vh;
      height: 100dvh;
      max-width: none;
      max-height: none;
      border-radius: 0;
      padding-top: env(safe-area-inset-top, 0px);
      padding-bottom: env(safe-area-inset-bottom, 0px);
    }

    .settings-modal-container {
      flex-direction: column;
      overflow: visible;
    }

    /* Hide full sidebar on mobile — use select dropdown */
    .settings-sidebar {
      display: none;
    }

    .mobile-section-nav {
      display: block;
    }

    .settings-header-bar {
      padding: 0.75rem 1rem;
    }

    .settings-content {
      padding: 1rem;
      flex: 1 1 0;
      min-height: 0;
      overflow-y: auto;
      -webkit-overflow-scrolling: touch;
      overflow-x: hidden;
    }

    /* Global mobile overrides for all settings panels */
    .settings-content :global(.form-group) {
      min-width: 0;
    }

    .settings-content :global(input),
    .settings-content :global(select),
    .settings-content :global(textarea) {
      min-height: 44px;
      font-size: 1rem;
      max-width: 100%;
      box-sizing: border-box;
    }

    .settings-content :global(button) {
      min-height: 44px;
    }

    .stats-grid {
      grid-template-columns: 1fr;
    }

    .section-header-row {
      flex-direction: column;
      align-items: flex-start;
      padding-right: 0;
      gap: 0.5rem;
    }

    .section-header-row .btn-refresh {
      width: 100%;
      justify-content: center;
      min-height: 44px;
    }

    .stat-card {
      padding: 0.75rem;
    }

    .stat-value {
      font-size: 1.25rem;
    }

    .model-value {
      word-break: break-all;
    }

    .action-buttons {
      flex-direction: column;
    }

    .action-buttons .btn {
      width: 100%;
      min-height: 44px;
      justify-content: center;
    }

    .task-health-grid .health-card {
      padding: 0.75rem;
    }

    .status-badge {
      padding: 0.1rem 0.35rem;
      font-size: 0.75rem;
      min-width: 24px;
      text-align: center;
    }

    .data-table th,
    .data-table td {
      padding: 0.4rem 0.5rem;
      font-size: 0.75rem;
    }

    .settings-subsection {
      padding: 0.75rem;
    }

    .form-actions {
      flex-direction: column-reverse;
    }

    .form-actions .btn {
      width: 100%;
      min-height: 44px;
      text-align: center;
    }

    .form-actions .btn-secondary {
      margin-right: 0;
    }
  }
</style>
