/**
 * Enhanced clipboard utility that works reliably across localhost and IP addresses
 * Provides fallback methods for secure/non-secure contexts
 */

import { get } from 'svelte/store';
import { t } from '$stores/locale';

export interface CopyResult {
  success: boolean;
  error?: string;
}

/**
 * Copy text to clipboard with IP address and non-secure context support
 * @param text - Text to copy to clipboard
 * @param onSuccess - Callback for successful copy
 * @param onError - Callback for copy failure
 */
export async function copyToClipboard(
  text: string,
  onSuccess?: () => void,
  onError?: (error: string) => void
): Promise<CopyResult> {
  if (!text) {
    const error = get(t)('clipboard.noContent');
    onError?.(error);
    return { success: false, error };
  }

  // Check if we're on IP address or non-localhost and use fallback immediately
  const isIPAddress = /^\d+\.\d+\.\d+\.\d+/.test(window.location.hostname);
  const isNonSecureContext = !window.isSecureContext;

  if (isIPAddress || isNonSecureContext || !navigator.clipboard) {
    return copyWithFallback(text, onSuccess, onError);
  }

  // Try modern clipboard API first
  try {
    await navigator.clipboard.writeText(text);
    onSuccess?.();
    return { success: true };
  } catch (err) {
    return copyWithFallback(text, onSuccess, onError);
  }
}

/**
 * Fallback copy method using execCommand for non-secure contexts
 */
function copyWithFallback(
  text: string,
  onSuccess?: () => void,
  onError?: (error: string) => void
): CopyResult {
  try {
    const textArea = document.createElement('textarea');
    textArea.value = text;
    textArea.style.position = 'fixed';
    textArea.style.left = '-999999px';
    textArea.style.top = '-999999px';
    textArea.style.opacity = '0';
    textArea.style.pointerEvents = 'none';
    textArea.setAttribute('readonly', '');
    document.body.appendChild(textArea);

    // Select the text
    textArea.focus();
    textArea.select();
    textArea.setSelectionRange(0, textArea.value.length);

    // Execute copy command
    const successful = document.execCommand('copy');
    document.body.removeChild(textArea);

    if (successful) {
      onSuccess?.();
      return { success: true };
    } else {
      throw new Error('execCommand copy failed');
    }
  } catch (fallbackError) {
    const error = get(t)('clipboard.operationFailed');
    onError?.(error);
    return { success: false, error };
  }
}
