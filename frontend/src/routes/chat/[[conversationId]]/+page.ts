import type { PageLoad } from './$types';

// Client-only, like the file detail route: the chat page is entirely
// authenticated, stateful and stream-driven — there is nothing meaningful to
// render on the server.
export const ssr = false;

export const load: PageLoad = ({ params, url }) => ({
  conversationId: params.conversationId ?? null,
  // Small selections can deep-link; large ones come through the store's
  // pendingContext handoff instead (URL length limits).
  fileUuids: url.searchParams.get('files')?.split(',').filter(Boolean) ?? [],
});
