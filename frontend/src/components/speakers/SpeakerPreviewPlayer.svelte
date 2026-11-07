<script lang="ts">
  import { t } from '$stores/locale';
  import { browser } from '$app/env';
  import type { SpeakerMediaPreviewData } from '$lib/api/speakerClusters';

  export let speakerPreviewData: SpeakerMediaPreviewData | null = null;
  export let previewCurrentTime = 0;
  export let playerRef: any = null;

  // Dynamic import, browser only: this route has no ssr=false guard and the player needs the
  // DOM. The Video.js modules import cleanly in Node, so the guard can go once a refresh of
  // /speakers is verified without it (issue #1327).
  let FloatingPreviewPlayer: typeof import('$components/FloatingPreviewPlayer.svelte').default | null = null;
  if (browser) {
    import('$components/FloatingPreviewPlayer.svelte').then(m => { FloatingPreviewPlayer = m.default; });
  }
</script>

{#if speakerPreviewData && FloatingPreviewPlayer}
  <svelte:component this={FloatingPreviewPlayer}
    bind:this={playerRef}
    title={speakerPreviewData.title}
    subtitle={speakerPreviewData.speaker_name}
    currentTime={previewCurrentTime}
    jumpToHref={`/files/${speakerPreviewData.file_uuid}?t=${previewCurrentTime || speakerPreviewData.start_time}`}
    closeTitle={$t('speakers.preview.close')}
    closeAriaLabel={$t('speakers.preview.closeAriaLabel')}
    mediaUrl={speakerPreviewData.media_url || ''}
    contentType={speakerPreviewData.content_type}
    startTime={speakerPreviewData.start_time}
    endTime={speakerPreviewData.end_time}
    fileId={speakerPreviewData.file_uuid}
    autoplay={true}
    on:close
    on:timeupdate
    on:play
    on:pause
  />
{/if}
