<script lang="ts">
  import type { MajorTopic } from '$lib/types/summary';
  import { t } from '$stores/locale';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';

  export let topics: MajorTopic[];
  // searchQuery prop removed - not used internally

  function getImportanceIndicator(importance: string): string {
    switch (importance) {
      case 'high': return '●';
      case 'medium': return '○';
      case 'low': return '◦';
      default: return '○';
    }
  }
</script>

<section class="topics-section">
  <h3 class="section-title">{$t('topics.majorTopics')}</h3>
  <div class="topics-list">
    {#each topics as topic}
      <div class="topic-item">
        <h4 class="topic-heading">
          <span class="importance-indicator importance-{topic.importance || 'medium'}">{getImportanceIndicator(topic.importance || 'medium')}</span>
          {@html sanitizeHighlightHtml(topic.topic)}
        </h4>

        {#if topic.participants && topic.participants.length > 0}
          <div class="topic-meta">
            <em>{$t('topics.keyParticipants')} {@html sanitizeHighlightHtml(topic.participants.join(', '))}</em>
          </div>
        {/if}

        {#if topic.key_points && topic.key_points.length > 0}
          <ul class="topic-points">
            {#each topic.key_points as point}
              <li>{@html sanitizeHighlightHtml(point)}</li>
            {/each}
          </ul>
        {/if}
      </div>
    {/each}
  </div>
</section>

<style>
  .topics-section {
    margin-bottom: 2rem;
  }

  .section-title {
    font-size: 1.25rem;
    font-weight: 600;
    margin-bottom: 0.75rem;
    color: var(--text-primary);
    border-bottom: 2px solid var(--border-color);
    padding-bottom: 0.5rem;
  }

  .topics-list {
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }

  .topic-item {
    border-left: 3px solid var(--border-color);
    padding-left: 1.25rem;
    padding-bottom: 1rem;
  }

  .topic-item:not(:last-child) {
    border-bottom: 1px solid var(--border-light);
  }

  .topic-heading {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 0.5rem 0;
    line-height: 1.3;
  }

  .importance-indicator {
    font-size: 0.8rem;
    margin-right: 0.25rem;
  }

  .importance-high {
    color: var(--error-color);
  }

  .importance-medium {
    color: var(--warning-color);
  }

  .importance-low {
    color: var(--text-muted);
  }

  .topic-meta {
    margin-bottom: 0.75rem;
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  .topic-points {
    margin: 0.75rem 0 0 0;
    padding-left: 1.25rem;
  }

  .topic-points li {
    line-height: 1.5;
    color: var(--text-secondary);
    margin-bottom: 0.5rem;
  }

  .topic-points li:last-child {
    margin-bottom: 0;
  }

  /* Clean list styling similar to markdown */
  .topic-points li::marker {
    color: var(--text-muted);
  }

  /* Search highlighting: .search-match / .current-match live in src/styles/search.css
     (shared across TranscriptModal, SummaryDisplay, TopicsList — see G7). */

  @media (max-width: 768px) {
    .topic-item {
      padding-left: 1rem;
    }

    .topic-heading {
      font-size: 1rem;
    }
  }
</style>
