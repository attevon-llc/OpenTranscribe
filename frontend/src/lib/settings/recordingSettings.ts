/**
 * Recording-settings load/save/reset for the Settings modal's Recording section.
 *
 * The modal owns the form fields and loading flag; these own the API calls, validation and the
 * toast wording. Each returns whether it succeeded so the caller can clear its dirty state.
 */
import {
  UserSettingsApi,
  RecordingSettingsHelper,
  type RecordingSettings,
} from '$lib/api/userSettings';
import { toastStore } from '$stores/toast';
import { getErrorMessage, handleApiError } from '$lib/utils/apiError';

type Translate = (key: string, options?: Record<string, unknown>) => string;

/** The saved settings, or null when the load failed (already reported). */
export async function loadRecording(t: Translate): Promise<RecordingSettings | null> {
  try {
    return await UserSettingsApi.getRecordingSettings();
  } catch (err: unknown) {
    console.error('Error loading recording settings:', err);
    // Via handleApiError, not a bare toast: this modal mounts before the first
    // account-lifecycle 403 lands, so a pending-approval user got a raw
    // "Request failed with status code 403" toast in front of the blocking
    // screen. handleApiError swallows lifecycle refusals the way it already
    // swallows cancellations.
    handleApiError(err, t('settings.toast.recordingSettingsSaveFailed'));
    return null;
  }
}

export async function saveRecording(settings: RecordingSettings, t: Translate): Promise<boolean> {
  const validationErrors = RecordingSettingsHelper.validateSettings(settings);
  const [firstError] = validationErrors;
  if (firstError !== undefined) {
    toastStore.error(firstError);
    return false;
  }
  try {
    await UserSettingsApi.updateRecordingSettings(settings);
    toastStore.success(t('settings.toast.recordingSettingsSaved'));
    return true;
  } catch (err: unknown) {
    console.error('Error saving recording settings:', err);
    toastStore.error(getErrorMessage(err, t('settings.toast.recordingSettingsSaveFailed')));
    return false;
  }
}

/** Resets server-side, then calls `reload` so the form shows the defaults. */
export async function resetRecording(t: Translate, reload: () => Promise<void>): Promise<boolean> {
  try {
    await UserSettingsApi.resetRecordingSettings();
    await reload();
    toastStore.success(t('settings.toast.recordingSettingsReset'));
    return true;
  } catch (err: unknown) {
    console.error('Error resetting recording settings:', err);
    toastStore.error(getErrorMessage(err, t('settings.toast.recordingSettingsResetFailed')));
    return false;
  }
}
