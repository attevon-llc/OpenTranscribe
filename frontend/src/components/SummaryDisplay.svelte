<script lang="ts">
  import type { SummaryData } from '$lib/types/summary';
  import TopicsList from './TopicsList.svelte';
  import { t } from '$stores/locale';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';

  export let summary: SummaryData;
  export let searchQuery: string = '';
  export let currentMatchIndex: number = 0;

  // Detect if this is a standard BLUF format or custom format
  $: isStandardBLUF = !!(summary.bluf && summary.brief_summary);

  // Create a function that generates all matches with proper indexing
  function highlightWithGlobalIndex(text: string, globalMatchIndex: { count: number }): string {
    if (!searchQuery || !text) return text;

    const escapedQuery = searchQuery.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const regex = new RegExp(`(${escapedQuery})`, 'gi');

    return text.replace(regex, (match) => {
      const isCurrentMatch = globalMatchIndex.count === currentMatchIndex;
      const thisMatchIndex = globalMatchIndex.count;
      globalMatchIndex.count++;

      if (isCurrentMatch) {
        return `<mark class="current-match" data-match-index="${thisMatchIndex}">${match}</mark>`;
      } else {
        return `<mark class="search-match" data-match-index="${thisMatchIndex}">${match}</mark>`;
      }
    });
  }

  // Highlighted rendering for custom formats with search
  function renderObjectWithHighlighting(obj: any, depth: number = 0): string {
    if (!searchQuery) {
      return renderObject(obj, depth);
    }

    const globalIndex = { count: 0 };
    return renderObjectWithIndex(obj, depth, globalIndex);
  }

  function renderObjectWithIndex(obj: any, depth: number, globalIndex: { count: number }): string {
    if (!obj || typeof obj !== 'object') return '';

    const entries = Object.entries(obj).filter(([key]) => key !== 'metadata');

    if (entries.length === 0) return '';

    return entries.map(([key, value]) => `
      <div class="field-group depth-${depth}">
        <div class="field-title">${escapeHtml(formatFieldName(key))}</div>
        <div class="field-content">${renderValueWithIndex(value, depth + 1, globalIndex)}</div>
      </div>
    `).join('');
  }

  function renderValueWithIndex(value: any, depth: number, globalIndex: { count: number }): string {
    if (value === null || value === undefined) return '';

    if (Array.isArray(value)) {
      if (value.length === 0) return `<em class="empty-list">${escapeHtml($t('summary.noItems'))}</em>`;

      const isObjectArray = value.some(item => typeof item === 'object' && item !== null);

      if (isObjectArray) {
        return value.map(item => renderObjectWithIndex(item, depth + 1, globalIndex)).join('');
      } else {
        // Simple list - highlight strings
        return '<ul class="simple-list">' +
          value.map(item => {
            const text = escapeHtml(String(item));
            return `<li>${highlightText(text, globalIndex)}</li>`;
          }).join('') +
          '</ul>';
      }
    }

    if (typeof value === 'object') {
      return renderObjectWithIndex(value, depth, globalIndex);
    }

    if (typeof value === 'boolean') {
      return value ? `<span class="bool-value">${escapeHtml($t('summary.yes'))}</span>` : `<span class="bool-value">${escapeHtml($t('summary.no'))}</span>`;
    }

    if (typeof value === 'number') {
      return `<span class="number-value">${value}</span>`;
    }

    // String value - highlight and preserve line breaks
    const escapedText = escapeHtml(String(value));
    const highlightedText = highlightText(escapedText, globalIndex);
    return highlightedText.replace(/\n/g, '<br>');
  }

  function highlightText(text: string, globalIndex: { count: number }): string {
    if (!searchQuery || !text) return text;

    const escapedQuery = searchQuery.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const regex = new RegExp(`(${escapedQuery})`, 'gi');

    return text.replace(regex, (match) => {
      const isCurrentMatch = globalIndex.count === currentMatchIndex;
      const thisMatchIndex = globalIndex.count;
      globalIndex.count++;

      if (isCurrentMatch) {
        return `<mark class="current-match" data-match-index="${thisMatchIndex}">${match}</mark>`;
      } else {
        return `<mark class="search-match" data-match-index="${thisMatchIndex}">${match}</mark>`;
      }
    });
  }

  // Recursive rendering for flexible structures
  function formatFieldName(key: string): string {
    // Convert snake_case or camelCase to Title Case
    return key
      .replace(/_/g, ' ')
      .replace(/([A-Z])/g, ' $1')
      .split(' ')
      .map(word => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');
  }

  function renderValue(value: any, depth: number = 0): string {
    if (value === null || value === undefined) return '';

    if (Array.isArray(value)) {
      if (value.length === 0) return `<em class="empty-list">${escapeHtml($t('summary.noItems'))}</em>`;

      // Check if array contains objects or primitives
      const isObjectArray = value.some(item => typeof item === 'object' && item !== null);

      if (isObjectArray) {
        return value.map(item => renderObject(item, depth + 1)).join('');
      } else {
        // Simple list of strings/numbers
        return '<ul class="simple-list">' +
          value.map(item => `<li>${escapeHtml(String(item))}</li>`).join('') +
          '</ul>';
      }
    }

    if (typeof value === 'object') {
      return renderObject(value, depth);
    }

    if (typeof value === 'boolean') {
      return value ? `<span class="bool-value">${escapeHtml($t('summary.yes'))}</span>` : `<span class="bool-value">${escapeHtml($t('summary.no'))}</span>`;
    }

    if (typeof value === 'number') {
      return `<span class="number-value">${value}</span>`;
    }

    // String value - preserve line breaks
    return escapeHtml(String(value)).replace(/\n/g, '<br>');
  }

  function renderObject(obj: any, depth: number = 0): string {
    if (!obj || typeof obj !== 'object') return '';

    const entries = Object.entries(obj).filter(([key]) => key !== 'metadata');

    if (entries.length === 0) return '';

    return entries.map(([key, value]) => `
      <div class="field-group depth-${depth}">
        <div class="field-title">${escapeHtml(formatFieldName(key))}</div>
        <div class="field-content">${renderValue(value, depth + 1)}</div>
      </div>
    `).join('');
  }

  function escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  // Extract text from value (handles both strings and objects)
  function extractText(value: any): string {
    if (typeof value === 'string') {
      return value;
    }
    if (typeof value === 'object' && value !== null) {
      // Try common field names for text content
      return value.decision || value.text || value.item || value.description || JSON.stringify(value);
    }
    return String(value);
  }

  // Action items are shape-tolerant: the DEFAULT summary prompt
  // (backend/app/core/default_prompts.py) emits {item, owner, due_date,
  // priority, context, mentioned_timestamp}; schemas/summary.py's ActionItem
  // model (exported but dead — nothing produces it) declares a DIFFERENT
  // shape, {text, assigned_to, ..., status}. SummaryData is extra="allow", so
  // a custom prompt may emit either, or neither. Try both spellings; never
  // assume one.
  function actionItemText(item: any): string {
    if (typeof item === 'string') return item;
    if (item && typeof item === 'object') {
      return item.item || item.text || item.description || '';
    }
    return '';
  }
  function actionItemOwner(item: any): string {
    if (item && typeof item === 'object') {
      return item.owner || item.assigned_to || '';
    }
    return '';
  }
  function actionItemDueDate(item: any): string {
    return item && typeof item === 'object' && item.due_date ? String(item.due_date) : '';
  }
  function actionItemPriority(item: any): string {
    return item && typeof item === 'object' && item.priority ? String(item.priority) : '';
  }

  // Speaker analysis: the default prompt's key is `speakers_analysis`, with
  // entries shaped {speaker, role, talk_time_percentage, key_contributions}.
  // `summary` field / `SpeakerInfo` shape ({name, percentage, key_points}) is
  // a legacy/alternate spelling this also tolerates.
  function speakerEntries(s: SummaryData): any[] {
    return (s.speakers_analysis as any[]) || (s.speakers as any[]) || [];
  }
  function speakerName(entry: any): string {
    return (entry && (entry.speaker || entry.name)) || '';
  }
  function speakerRole(entry: any): string {
    return (entry && entry.role) || '';
  }
  function speakerTalkTime(entry: any): number | null {
    const pct = entry && (entry.talk_time_percentage ?? entry.percentage);
    return pct === undefined || pct === null ? null : Number(pct);
  }
  function speakerPoints(entry: any): string[] {
    return (entry && (entry.key_contributions || entry.key_points)) || [];
  }

  // Reactive function to generate highlighted content (for BLUF format)
  function getHighlightedContent() {
    if (!searchQuery || !summary) return null;

    const globalIndex = { count: 0 };

    return {
      bluf: summary.bluf ? highlightWithGlobalIndex(escapeHtml(summary.bluf), globalIndex) : null,
      briefSummary: summary.brief_summary ? highlightWithGlobalIndex(escapeHtml(summary.brief_summary), globalIndex) : null,
      keyDecisions: (summary.key_decisions || []).map(decision =>
        highlightWithGlobalIndex(escapeHtml(extractText(decision)), globalIndex)
      ),
      followUpItems: (summary.follow_up_items || []).map(item =>
        highlightWithGlobalIndex(escapeHtml(extractText(item)), globalIndex)
      ),
      majorTopics: (summary.major_topics || []).map(topic => ({
        ...topic,
        topic: highlightWithGlobalIndex(escapeHtml(topic.topic || ''), globalIndex),
        key_points: (topic.key_points || []).map(point => highlightWithGlobalIndex(escapeHtml(point || ''), globalIndex)),
        participants: (topic.participants || []).map(p => highlightWithGlobalIndex(escapeHtml(p || ''), globalIndex))
      })),
      actionItems: (summary.action_items || []).map(item => ({
        text: highlightWithGlobalIndex(escapeHtml(actionItemText(item)), globalIndex),
        owner: actionItemOwner(item) ? highlightWithGlobalIndex(escapeHtml(actionItemOwner(item)), globalIndex) : '',
        dueDate: actionItemDueDate(item) ? escapeHtml(actionItemDueDate(item)) : '',
        priority: actionItemPriority(item)
      })),
      speakersAnalysis: speakerEntries(summary).map(entry => ({
        name: highlightWithGlobalIndex(escapeHtml(speakerName(entry)), globalIndex),
        role: speakerRole(entry) ? escapeHtml(speakerRole(entry)) : '',
        talkTime: speakerTalkTime(entry),
        points: speakerPoints(entry).map((p: string) => highlightWithGlobalIndex(escapeHtml(p || ''), globalIndex))
      }))
    };
  }

  // Pre-escape topics for the non-highlighted path
  $: escapedTopics = (summary.major_topics || []).map(topic => ({
    ...topic,
    topic: escapeHtml(topic.topic || ''),
    key_points: (topic.key_points || []).map((p: string) => escapeHtml(p || '')),
    participants: (topic.participants || []).map((p: string) => escapeHtml(p || ''))
  }));

  // Pre-escape action items / speaker analysis for the non-highlighted path.
  // Neither is rendered anywhere else in this modal today, so this is the
  // ONLY renderer for them — a per-file recording artifact the product
  // otherwise never shows.
  $: escapedActionItems = (summary.action_items || []).map(item => ({
    text: escapeHtml(actionItemText(item)),
    owner: escapeHtml(actionItemOwner(item)),
    dueDate: escapeHtml(actionItemDueDate(item)),
    priority: actionItemPriority(item)
  }));

  $: escapedSpeakersAnalysis = speakerEntries(summary).map(entry => ({
    name: escapeHtml(speakerName(entry)),
    role: escapeHtml(speakerRole(entry)),
    talkTime: speakerTalkTime(entry),
    points: speakerPoints(entry).map((p: string) => escapeHtml(p || ''))
  }));

  let highlightedContent: any = null;
  let customHighlightedContent: string = '';

  // Reactive statement that triggers when searchQuery OR currentMatchIndex changes
  $: {
    // Reference these variables to track changes (Svelte reactivity)
    void searchQuery;
    void currentMatchIndex;
    void summary;

    if (summary) {
      if (searchQuery) {
        highlightedContent = getHighlightedContent();
        customHighlightedContent = renderObjectWithHighlighting(summary, 0);
      } else {
        highlightedContent = null;
        customHighlightedContent = renderObject(summary, 0);
      }
    }
  }
</script>

<div class="summary-content">
  {#if isStandardBLUF}
    <!-- Standard BLUF Format Display -->
    <section class="bluf-section">
      <h3 class="section-title">{$t('summary.executiveSummary')}</h3>
      <div class="bluf-content">
        {@html sanitizeHighlightHtml(highlightedContent?.bluf || escapeHtml(summary.bluf || ''))}
      </div>
    </section>

    <section class="brief-summary-section">
      <h3 class="section-title">{$t('summary.briefSummary')}</h3>
      <div class="brief-summary-content">
        {@html sanitizeHighlightHtml(highlightedContent?.briefSummary || escapeHtml(summary.brief_summary || ''))}
      </div>
    </section>

    {#if summary.major_topics && summary.major_topics.length > 0}
      <TopicsList
        topics={highlightedContent?.majorTopics || escapedTopics}
      />
    {/if}

    {#if summary.action_items && summary.action_items.length > 0}
      <section class="action-items-section">
        <h3 class="section-title">{$t('summary.actionItems')}</h3>
        <div class="action-items-list">
          {#each (highlightedContent?.actionItems || escapedActionItems) as item}
            <div class="action-item">
              <div class="action-item-bullet">☐</div>
              <div class="action-item-body">
                <div class="action-item-text">{@html sanitizeHighlightHtml(item.text)}</div>
                {#if item.owner || item.dueDate || item.priority}
                  <div class="action-item-meta">
                    {#if item.owner}
                      <span class="action-item-owner">{$t('summary.owner')}: {@html sanitizeHighlightHtml(item.owner)}</span>
                    {/if}
                    {#if item.dueDate}
                      <span class="action-item-due">{$t('summary.dueDate')}: {@html sanitizeHighlightHtml(item.dueDate)}</span>
                    {/if}
                    {#if item.priority}
                      <span class="action-item-priority priority-{item.priority}">{item.priority}</span>
                    {/if}
                  </div>
                {/if}
              </div>
            </div>
          {/each}
        </div>
      </section>
    {/if}

    {#if summary.key_decisions && summary.key_decisions.length > 0}
      <section class="key-decisions-section">
        <h3 class="section-title">{$t('summary.keyDecisions')}</h3>
        <div class="key-decisions-list">
          {#each (highlightedContent?.keyDecisions || summary.key_decisions) as decision}
            <div class="key-decision-item">
              <div class="decision-bullet">✓</div>
              <div class="decision-text">{@html sanitizeHighlightHtml(highlightedContent ? (decision as string) : escapeHtml(extractText(decision)))}</div>
            </div>
          {/each}
        </div>
      </section>
    {/if}

    {#if speakerEntries(summary).length > 0}
      <section class="speaker-analysis-section">
        <h3 class="section-title">{$t('summary.speakerAnalysis')}</h3>
        <div class="speakers-list">
          {#each (highlightedContent?.speakersAnalysis || escapedSpeakersAnalysis) as s}
            <div class="speaker-item">
              <div class="speaker-header">
                <span class="speaker-name">{@html sanitizeHighlightHtml(s.name)}</span>
                {#if s.role}<span class="speaker-role">{@html sanitizeHighlightHtml(s.role)}</span>{/if}
                {#if s.talkTime !== null && s.talkTime !== undefined}
                  <span class="speaker-talktime">{s.talkTime}%</span>
                {/if}
              </div>
              {#if s.points && s.points.length > 0}
                <ul class="speaker-points">
                  {#each s.points as point}
                    <li>{@html sanitizeHighlightHtml(point)}</li>
                  {/each}
                </ul>
              {/if}
            </div>
          {/each}
        </div>
      </section>
    {/if}

    {#if summary.follow_up_items && summary.follow_up_items.length > 0}
      <section class="follow-up-section">
        <h3 class="section-title">{$t('summary.followUpItems')}</h3>
        <div class="follow-up-list">
          {#each (highlightedContent?.followUpItems || summary.follow_up_items) as item}
            <div class="follow-up-item">
              <div class="follow-up-bullet">→</div>
              <div class="follow-up-text">{@html sanitizeHighlightHtml(highlightedContent ? (item as string) : escapeHtml(extractText(item)))}</div>
            </div>
          {/each}
        </div>
      </section>
    {/if}
  {:else}
    <!-- Flexible Custom Format Display -->
    <div class="custom-summary">
      {@html sanitizeHighlightHtml(customHighlightedContent)}
    </div>
  {/if}

  <!-- AI Disclaimer -->
  <section class="ai-disclaimer-section">
    <div class="ai-disclaimer">
      <p class="disclaimer-text">
        {$t('summary.aiDisclaimer')}
        {#if summary.metadata}
          {$t('summary.generatedBy', { provider: summary.metadata.provider, model: summary.metadata.model })}
        {/if}
      </p>
    </div>
  </section>
</div>

<style>
  .summary-content {
    padding: 1.5rem;
    max-height: calc(100vh - 200px);
    max-height: calc(100dvh - 200px);
    overflow-y: auto;
  }

  .section-title {
    font-size: 1.25rem;
    font-weight: 600;
    margin-bottom: 0.75rem;
    color: var(--text-primary);
    border-bottom: 2px solid var(--border-color);
    padding-bottom: 0.5rem;
  }

  .bluf-section, .brief-summary-section {
    margin-bottom: 2rem;
  }

  /* Add consistent spacing between all sections */
  section {
    margin-bottom: 2rem;
  }

  section:last-child {
    margin-bottom: 1rem;
  }

  .bluf-content, .brief-summary-content {
    line-height: 1.6;
    color: var(--text-secondary);
  }


  .key-decisions-list, .follow-up-list {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .key-decision-item, .follow-up-item {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
  }

  .decision-bullet, .follow-up-bullet {
    color: var(--success-color);
    font-weight: 600;
    margin-top: 0.1rem;
  }

  .decision-text, .follow-up-text {
    flex: 1;
    line-height: 1.5;
  }

  .action-items-list {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .action-item {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
  }

  .action-item-bullet {
    color: var(--primary-color);
    font-weight: 600;
    margin-top: 0.1rem;
  }

  .action-item-body {
    flex: 1;
  }

  .action-item-text {
    line-height: 1.5;
  }

  .action-item-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    margin-top: 0.25rem;
    font-size: 0.85rem;
    color: var(--text-muted);
  }

  .action-item-priority {
    text-transform: uppercase;
    font-weight: 600;
    padding: 0.05rem 0.4rem;
    border-radius: 3px;
  }

  .action-item-priority.priority-high {
    color: var(--danger-color, #d32f2f);
  }

  .action-item-priority.priority-medium {
    color: var(--warning-color, #ed6c02);
  }

  .action-item-priority.priority-low {
    color: var(--text-muted);
  }

  .speakers-list {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .speaker-item {
    padding-left: 0.75rem;
    border-left: 2px solid var(--border-color);
  }

  .speaker-header {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .speaker-name {
    font-weight: 600;
    color: var(--text-primary);
  }

  .speaker-role {
    color: var(--text-muted);
    font-style: italic;
    font-size: 0.9rem;
  }

  .speaker-talktime {
    color: var(--primary-color);
    font-weight: 500;
    font-size: 0.85rem;
  }

  .speaker-points {
    margin: 0.4rem 0 0;
    padding-left: 1.25rem;
  }

  .speaker-points li {
    margin-bottom: 0.2rem;
    line-height: 1.5;
    color: var(--text-secondary);
  }

  .ai-disclaimer-section {
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color);
  }

  .ai-disclaimer {
    text-align: center;
  }

  .disclaimer-text {
    font-size: 0.8rem;
    color: var(--text-muted);
    font-style: italic;
    margin: 0;
    line-height: 1.4;
  }

  /* Custom Summary Styles */
  .custom-summary {
    padding: 0.5rem 0;
  }

  :global(.field-group) {
    margin-bottom: 1.5rem;
    padding-left: 1rem;
  }

  :global(.field-group.depth-0) {
    border-left: 3px solid var(--primary-color);
    padding-left: 1rem;
    margin-bottom: 2rem;
  }

  :global(.field-group.depth-1) {
    border-left: 2px solid var(--border-color);
    padding-left: 0.75rem;
  }

  :global(.field-group.depth-2) {
    border-left: 1px solid var(--border-color);
    padding-left: 0.5rem;
  }

  :global(.field-title) {
    font-size: 1.1rem;
    font-weight: 600;
    margin-bottom: 0.5rem;
    color: var(--text-primary);
  }

  :global(.field-content) {
    color: var(--text-secondary);
    line-height: 1.6;
  }

  :global(.simple-list) {
    margin: 0.5rem 0;
    padding-left: 1.5rem;
  }

  :global(.simple-list li) {
    margin-bottom: 0.25rem;
    line-height: 1.5;
  }

  :global(.empty-list) {
    color: var(--text-tertiary, #999);
    font-style: italic;
  }

  :global(.bool-value) {
    font-weight: 500;
    color: var(--success-color);
  }

  :global(.number-value) {
    font-weight: 500;
    color: var(--primary-color);
  }

  /* Search highlighting styles */
  :global(.search-match) {
    background-color: #ffeb3b;
    color: #000;
    padding: 0.1rem 0.2rem;
    border-radius: 3px;
    font-weight: 500;
  }

  :global(.current-match) {
    background-color: #ff9800;
    color: #000;
    padding: 0.1rem 0.2rem;
    border-radius: 3px;
    font-weight: 600;
    box-shadow: 0 0 0 2px rgba(255, 152, 0, 0.3);
  }
</style>
