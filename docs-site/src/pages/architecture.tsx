import Layout from '@theme/Layout';
import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';
import useBaseUrl from '@docusaurus/useBaseUrl';
import React, {type JSX} from 'react';

import styles from './architecture.module.css';

/**
 * Interactive architecture diagrams.
 *
 * Unlike roadmap.tsx, these are NOT derived from an external tracker — there is no
 * "architecture tracker" to derive from. Each diagram is a hand-authored Archify
 * spec under docs-site/architecture-diagrams/specs/, built from real repo evidence
 * (routers, Celery task_routes, services/ layout, frontend directory structure) and
 * regenerated into docs-site/static/architecture/*.html by
 * scripts/generate-architecture-diagrams.sh. Treat a diagram going stale the same
 * way as any other doc: fix the spec when the code it describes changes.
 */

type Diagram = {
  id: string;
  title: string;
  description: string;
};

type Group = {
  id: string;
  label: string;
  diagrams: Diagram[];
};

const GROUPS: Group[] = [
  {
    id: 'system',
    label: 'System',
    diagrams: [
      {
        id: 'system-overview',
        title: 'System Overview',
        description:
          'The full stack in one diagram: SvelteKit SPA, Nginx, FastAPI backend, Celery workers, Postgres, OpenSearch, MinIO, and the LLM provider. Start here.',
      },
      {
        id: 'celery-queues',
        title: 'Celery Queues',
        description:
          'The 8 real queues behind "Celery Workers": gpu, cpu, nlp, embedding, redaction, utility, download, and the dynamic cloud-asr/cpu-transcribe queues, with what runs on each and why.',
      },
    ],
  },
  {
    id: 'pipelines',
    label: 'Pipelines & Workflows',
    diagrams: [
      {
        id: 'transcription-pipeline',
        title: 'Transcription Pipeline',
        description:
          'Upload through GPU-queue ASR/diarization to post-processing, indexing, and delivery.',
      },
      {
        id: 'transcription-enrichment',
        title: 'Post-Transcription Enrichment',
        description:
          'What happens after a transcript is finalized: voiceprinting, gender/age detection, LLM speaker-ID suggestions, summarization, topic extraction, and analytics.',
      },
      {
        id: 'cloud-asr',
        title: 'Cloud ASR + Voiceprinting',
        description:
          'How the 8 supported cloud ASR providers plug into one factory, and how voiceprinting still happens when a provider supplies no native embeddings.',
      },
      {
        id: 'speaker-clustering',
        title: 'Speaker Clustering & Naming',
        description:
          'From a raw voiceprint to automatic profile matching, cross-file clustering, and human-reviewed promotion to a named speaker.',
      },
      {
        id: 'search-indexing',
        title: 'Search & Indexing',
        description:
          'The query path (BM25 + kNN fused via RRF, then quarantine/redaction filtering) and the write path (chunking + embedding) in one diagram.',
      },
      {
        id: 'rag-chat',
        title: 'RAG / Chat Retrieval',
        description:
          "Query → scope/plan → retrieval → the local-vs-remote redaction guard → LLM → output redaction → streamed answer with citations.",
      },
    ],
  },
  {
    id: 'codebase',
    label: 'Codebase Maps',
    diagrams: [
      {
        id: 'backend-modules',
        title: 'Backend Module Map',
        description:
          'How backend/app/{api,auth,schemas,services,tasks,models,db,core,utils} actually relate: one services/ layer called by both the API and Celery.',
      },
      {
        id: 'frontend-modules',
        title: 'Frontend Module Map',
        description:
          "How the SvelteKit frontend's routes/, components/, stores/, and lib/ fit together, including the community/cloud seam.",
      },
    ],
  },
];

// Archify's viewer shell is `min-height: 100dvh` with its own internal scrolling —
// it is not a normal flowing document. Reading contentDocument.scrollHeight to
// auto-fit the iframe seemed like the right move, but it creates a feedback loop:
// growing the iframe grows `dvh`, which grows the next scrollHeight reading, so the
// frame ratchets upward on every diagram that has one. A fixed, hand-verified
// height per diagram is the stable option instead — each value below is the
// measured bounding-box bottom of that diagram's real content (panel + legend + 3
// info cards) at this page's 1152px content width, plus a small buffer. Re-measure
// with a headless browser if a spec's node/card count changes materially.
const FRAME_HEIGHT: Record<string, number> = {
  'backend-modules': 940,
  'celery-queues': 1100,
  'cloud-asr': 1150,
  'frontend-modules': 1010,
  'rag-chat': 1160,
  'search-indexing': 1010,
  'speaker-clustering': 1280,
  'system-overview': 950,
  'transcription-enrichment': 1250,
  'transcription-pipeline': 950,
};
const DEFAULT_FRAME_HEIGHT = 1000;

function DiagramFrame({id, title}: {id: string; title: string}): JSX.Element {
  return (
    <div className={styles.frame}>
      <iframe
        src={useBaseUrl(`/architecture/${id}.html`)}
        title={title}
        className={styles.iframe}
        style={{height: `${FRAME_HEIGHT[id] ?? DEFAULT_FRAME_HEIGHT}px`}}
      />
    </div>
  );
}

function GroupPanel({group}: {group: Group}): JSX.Element {
  return (
    <Tabs groupId={`architecture-${group.id}`} className={styles.innerTabs}>
      {group.diagrams.map((d) => (
        <TabItem key={d.id} value={d.id} label={d.title}>
          <p className={styles.diagramDescription}>{d.description}</p>
          <DiagramFrame id={d.id} title={d.title} />
        </TabItem>
      ))}
    </Tabs>
  );
}

export default function Architecture(): JSX.Element {
  return (
    <Layout
      title="Architecture"
      description="Interactive, evidence-based architecture diagrams for OpenTranscribe: system overview, Celery queues, transcription and RAG pipelines, and codebase maps."
    >
      <header className={styles.hero}>
        <h1 className={styles.title}>Architecture</h1>
        <p className={styles.subtitle}>
          Interactive diagrams built from the real codebase. Pan, zoom, search, and click
          through guided views inside each one. Not hand-drawn illustrations: every diagram is a
          validated spec checked against the actual routers, Celery queue routes, and directory
          structure.
        </p>
      </header>

      <main className={styles.main}>
        <Tabs groupId="architecture-section" className={styles.outerTabs}>
          {GROUPS.map((group) => (
            <TabItem key={group.id} value={group.id} label={group.label}>
              <GroupPanel group={group} />
            </TabItem>
          ))}
        </Tabs>
      </main>

      <footer className={styles.footnote}>
        <p>
          Every diagram here is rendered by{' '}
          <a href="https://github.com/tt-a1i/archify">Archify</a> (MIT, © 2026 tt-a1i / Archify,
          © 2025 Cocoon AI).
        </p>
      </footer>
    </Layout>
  );
}
