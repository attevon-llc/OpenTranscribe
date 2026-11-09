/**
 * The toast shown when a file's processing failed, built from the backend's error categorisation.
 */
import { resolveMediaErrorI18nKey } from '$lib/i18n/mediaErrors';
import { toastStore } from '$stores/toast';
import type { MediaFile } from '$lib/types/media';

type Translate = (key: string, options?: Record<string, unknown>) => string;

export function showFileErrorToast(file: MediaFile, t: Translate): void {
  if (file.error_reason && file.error_suggestions) {
    const suggestions = file.error_suggestions.map((s) => `\u2022 ${s}`).join('\n');
    // GH #960: translate the fixed backend reason client-side; fall back to the server-authored
    // English `user_message` only for a reason this client doesn't know.
    const mediaErrorI18nKey = resolveMediaErrorI18nKey(file.error_reason);
    const message = mediaErrorI18nKey
      ? t(mediaErrorI18nKey)
      : file.user_message ||
        t('gallery.processingFailed', { filename: file.title || file.filename });
    toastStore.error(
      t('gallery.processingFailedWithSuggestions', { message, suggestions }),
      file.error_reason === 'file_quality' ? 10000 : 8000
    );
  } else {
    // Minimal fallback for unexpected cases
    toastStore.error(t('gallery.processingFailed', { filename: file.title || file.filename }));
  }
}
