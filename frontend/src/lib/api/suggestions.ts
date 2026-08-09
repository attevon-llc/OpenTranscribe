/**
 * API client for AI suggestions (tags and collections)
 */
import axiosInstance from '../axios';

export interface TagSuggestion {
  name: string;
  confidence: number;
  rationale?: string;
}

export interface CollectionSuggestion {
  name: string;
  confidence: number;
  rationale?: string;
  description?: string;
}

export interface AISuggestions {
  tags: TagSuggestion[];
  collections: CollectionSuggestion[];
  status: 'pending' | 'accepted' | 'rejected';
  suggestion_id?: string; // UUID
}

/** Raw tag suggestion shape as returned by the backend, before normalisation. */
interface RawTagSuggestion {
  name: string;
  confidence?: number;
  rationale?: string;
}

/** Raw collection suggestion shape as returned by the backend, before normalisation. */
interface RawCollectionSuggestion {
  name: string;
  confidence?: number;
  rationale?: string;
  description?: string;
}

/**
 * Get AI suggestions for a file (tags and collections)
 */
export async function getAISuggestions(fileId: string): Promise<AISuggestions | null> {
  try {
    const response = await axiosInstance.get(`/files/${fileId}/suggestions`);

    if (!response.data) {
      return null;
    }

    const data = response.data;

    // Extract tags and collections from the response
    const tags: TagSuggestion[] = (data.suggested_tags || []).map((tag: RawTagSuggestion) => ({
      name: tag.name,
      confidence: tag.confidence || 0.5,
      rationale: tag.rationale,
    }));

    const collections: CollectionSuggestion[] = (data.suggested_collections || []).map(
      (col: RawCollectionSuggestion) => ({
        name: col.name,
        confidence: col.confidence || 0.5,
        rationale: col.rationale,
        description: col.description,
      })
    );

    return {
      tags,
      collections,
      status: data.status || 'pending',
      suggestion_id: data.uuid || data.suggestion_id || data.id,
    };
  } catch (error: unknown) {
    const status = (error as { response?: { status?: number } })?.response?.status;
    if (status === 404) {
      // No suggestions yet
      return null;
    }
    console.error('Error fetching AI suggestions:', error);
    throw error;
  }
}

/**
 * Trigger AI suggestion extraction for a file
 */
export async function extractAISuggestions(
  fileId: string,
  forceRegenerate: boolean = false
): Promise<void> {
  await axiosInstance.post(`/files/${fileId}/extract`, {
    force_regenerate: forceRegenerate,
  });
}
