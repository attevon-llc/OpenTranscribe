import type { PageLoad } from './$types';

// SPA mode: the page builds on browser-only APIs (media elements, Video.js custom elements)
export const ssr = false;

export const load: PageLoad = ({ params }) => {
  return {
    id: params.id,
  };
};
