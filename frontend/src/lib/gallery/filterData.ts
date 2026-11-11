/**
 * The filter sidebar's data fetches (tags, owners, speakers, library metadata).
 *
 * Each loader resolves to plain data and THROWS on failure: every facet fetch fails visibly,
 * because an empty list is indistinguishable from "you have none of these", so a 500 used to
 * render as a normal, empty filter with no error and no way to retry (#743a). The component
 * owns the loading/error state; these own the request shapes.
 */
import axiosInstance, { isRequestCancelled } from '$lib/axios';
import { listTags } from '$lib/api/tags';
import { apiCache, cacheKey, CacheTTL } from '$lib/apiCache';
import { fetchMetadataFilters } from '$lib/api/metadataFilters';
import type { TagWithCount } from '$lib/types/tag';

/** Owners the caller can already see (issue #966): `GET /files/owners`, same shape as the sharing picker. */
export type OwnerOption = { uuid: string; full_name: string | null; masked_email: string };

/** Turn a caught value into something renderable, without swallowing it. */
export function describeError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** What one facet load means for the section that started it. */
export type FacetOutcome<T> =
  | { kind: 'ok'; value: T }
  | { kind: 'error'; message: string; cause: unknown }
  /** Cancelled (sign-out): not a failure, and there is nothing new to show. */
  | { kind: 'cancelled' }
  /** A newer load of the same facet started after this one: it owns the section's state. */
  | { kind: 'superseded' };

/**
 * Wrap a facet loader so only its NEWEST call decides what the section shows.
 *
 * A facet reloads on mount, on a push invalidation and on every search, so two loads overlap,
 * and they can settle in either order. Without this, an older load that settled last replaced
 * the newer list with stale rows, or put the error state over a list that had just loaded, and a
 * request cancelled at sign-out rendered as "could not load".
 */
export function latestFacetLoad<A extends unknown[], T>(
  load: (...args: A) => Promise<T>
): (...args: A) => Promise<FacetOutcome<T>> {
  let generation = 0;
  return async (...args: A) => {
    const mine = ++generation;
    try {
      const value = await load(...args);
      return mine === generation ? { kind: 'ok', value } : { kind: 'superseded' };
    } catch (err) {
      if (mine !== generation) return { kind: 'superseded' };
      if (isRequestCancelled(err)) return { kind: 'cancelled' };
      return { kind: 'error', message: describeError(err), cause: err };
    }
  };
}

/** Prefer the display name; fall back to the masked email, never a raw one. */
export const ownerLabel = (owner: OwnerOption): string => owner.full_name || owner.masked_email;

/** The string the API offers a speaker under, and the value sent back as `?speaker=`. */
export const speakerLabel = (speaker: { display_name?: string | null; name: string }): string =>
  speaker.display_name || speaker.name;

/** Tags (cached with TTL, invalidated via WebSocket push). */
export function loadTags(): Promise<TagWithCount[]> {
  return apiCache.getOrFetch(cacheKey.tags(), () => listTags(), CacheTTL.TAGS);
}

/**
 * The owners of files the caller can already see (issue #966).
 *
 * `GET /files/owners` is a CLOSED enumeration with no `q` parameter, so the (capped) full list is
 * fetched once and filtered client-side. A free-text parameter on this endpoint would turn an
 * authorized "owners of files you can see" list into an account-probing oracle.
 */
export async function loadOwners(): Promise<OwnerOption[]> {
  const rows = await apiCache.getOrFetch(
    cacheKey.owners(),
    async () => {
      const response = await axiosInstance.get('/files/owners');
      return response.data;
    },
    CacheTTL.OWNERS
  );
  return Array.isArray(rows) ? rows : [];
}

/** A speaker row from `GET /speakers?for_filter=true`. */
export interface FilterSpeaker {
  uuid: string;
  name: string;
  display_name?: string | null;
  is_unnamed?: boolean;
  media_count?: number;
}

export const SPEAKER_FILTER_PAGE_SIZE = 200;

/**
 * Speakers for filtering, with server-side type-to-search (`GET /speakers?for_filter=true&q=...`).
 *
 * `include_unnamed` asks for speakers nobody has named yet (#743). `limit` is passed explicitly,
 * one MORE than displayed: an over-full page is the only exact way to tell "this was cut" from
 * "this is all of them" (#743b).
 */
export async function loadSpeakers(
  q = ''
): Promise<{ speakers: FilterSpeaker[]; truncated: boolean }> {
  const rows = await apiCache.getOrFetch(
    cacheKey.speakers(q || undefined),
    async () => {
      const response = await axiosInstance.get('/speakers', {
        params: {
          for_filter: true,
          q: q.trim() || undefined,
          include_unnamed: true,
          limit: SPEAKER_FILTER_PAGE_SIZE + 1,
        },
      });
      return response.data;
    },
    CacheTTL.SPEAKERS
  );
  const list = Array.isArray(rows) ? rows : [];
  return {
    truncated: list.length > SPEAKER_FILTER_PAGE_SIZE,
    speakers: list.slice(0, SPEAKER_FILTER_PAGE_SIZE),
  };
}

export interface LibraryMetadata {
  /** Real duration bounds in seconds, or null when the library reports none. */
  durationBounds: { min: number; max: number } | null;
  languages: string[];
}

/** The library's real duration bounds and transcript languages (`/files/metadata-filters`). */
export async function loadLibraryMetadata(): Promise<LibraryMetadata> {
  const data = await fetchMetadataFilters();
  let durationBounds: LibraryMetadata['durationBounds'] = null;
  if (data.duration) {
    const minDur = Math.floor(data.duration.min ?? 0);
    const maxDur = Math.ceil(data.duration.max ?? 0);
    durationBounds = { min: minDur, max: Math.max(maxDur, minDur + 60) };
  }
  return { durationBounds, languages: Array.isArray(data.languages) ? data.languages : [] };
}
