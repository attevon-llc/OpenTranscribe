import { writable } from 'svelte/store';
import type { SelfErasureReceipt } from '$lib/api/selfErasure';

/**
 * The signed-in person's open erasure request, shared by the Settings card (which changes it)
 * and the app-level banner (which only shows it). `undefined` = not read yet, `null` = none.
 */
export const openErasureRequest = writable<SelfErasureReceipt | null | undefined>(undefined);
