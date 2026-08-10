import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

import styles from './index.module.css';

/* ===== SVG Icons (Lucide-style, hand-picked) ===== */

function IconMic() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" x2="12" y1="19" y2="22" />
    </svg>
  );
}

function IconUsers() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  );
}

function IconSparkles() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z" />
      <path d="M20 3v4" />
      <path d="M22 5h-4" />
    </svg>
  );
}

function IconSearch() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.3-4.3" />
    </svg>
  );
}

function IconShield() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}

function IconServer() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect width="20" height="8" x="2" y="2" rx="2" ry="2" />
      <rect width="20" height="8" x="2" y="14" rx="2" ry="2" />
      <line x1="6" x2="6.01" y1="6" y2="6" />
      <line x1="6" x2="6.01" y1="18" y2="18" />
    </svg>
  );
}

function IconGlobe() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20" />
      <path d="M2 12h20" />
    </svg>
  );
}

function IconZap() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z" />
    </svg>
  );
}

function IconArrowRight() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 12h14" />
      <path d="m12 5 7 7-7 7" />
    </svg>
  );
}

function IconGithub() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z" />
    </svg>
  );
}

// `currentColor` + a themed class rather than a hardcoded hex: #2563eb is the light
// primary and drops to poor contrast against the dark background.
function Check() {
  return (
    <svg
      className={styles.check}
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="3"
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
      aria-label="Supported">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

function Dash() {
  return (
    <span className={styles.dash} role="img" aria-label="Not supported">
      --
    </span>
  );
}

/* ===== DATA ===== */

const features = [
  {
    title: 'Transcription',
    icon: <IconMic />,
    desc: 'WhisperX with word-level timestamps across 100+ languages. Native cross-attention alignment, 70x realtime on GPU, configurable models.',
  },
  {
    title: 'Speaker Identification',
    icon: <IconUsers />,
    desc: 'PyAnnote v4 diarization with cross-video voice fingerprinting. GPU-accelerated clustering, gender classification, and automatic profile matching.',
  },
  {
    title: 'AI Analysis',
    icon: <IconSparkles />,
    desc: 'BLUF summaries, action item extraction, auto-labeling. Works with OpenAI, Claude, vLLM, Ollama, or OpenRouter. Bring your own model.',
  },
  {
    title: 'Hybrid Search',
    icon: <IconSearch />,
    desc: 'Full-text and semantic search across all transcripts. BM25 + neural vectors merged via Reciprocal Rank Fusion on OpenSearch.',
  },
  {
    title: 'Enterprise Auth',
    icon: <IconShield />,
    desc: 'LDAP/AD, OpenID Connect, PKI/X.509, MFA. FedRAMP-aligned controls, AES-256-GCM encryption, audit logging, FIPS 140-3 ready.',
  },
  {
    title: 'Self-Hosted',
    icon: <IconServer />,
    desc: 'Docker Compose deployment. Multi-GPU scaling, 3-stage Celery pipeline, health monitoring. Runs air-gapped. Your data, your servers.',
  },
];

const useCases = [
  {title: 'Meetings', desc: 'Speaker-attributed transcripts with action items', icon: <IconUsers />},
  {title: 'Media Production', desc: 'Subtitles, indexing, and podcast transcription', icon: <IconMic />},
  {title: 'Legal & Compliance', desc: 'Depositions, audit trails, FIPS compliance', icon: <IconShield />},
  {title: 'Research', desc: 'Interview analysis and multilingual support', icon: <IconGlobe />},
  {title: 'Government', desc: 'Air-gapped, PKI auth, classification banners', icon: <IconServer />},
  {title: 'Call Centers', desc: 'High-volume analysis and trend detection', icon: <IconSparkles />},
];

/* ===== SECTIONS ===== */

function Hero() {
  const {siteConfig} = useDocusaurusContext();
  const version = siteConfig.customFields?.version as string;
  const githubRepo = siteConfig.customFields?.githubRepo as string;

  return (
    <header className={styles.heroBanner}>
      <div className={clsx('container', styles.heroContent)}>
        <div className={styles.heroBadges}>
          {version && <span className={styles.badge}>{`v${version}`}</span>}
          <span className={styles.badge}>AGPL-3.0</span>
          <span className={styles.badge}>Self-Hosted</span>
        </div>

        <Heading as="h1" className={styles.heroTitle}>
          OpenTranscribe
        </Heading>
        <p className={styles.heroTagline}>
          AI-powered transcription with speaker identification, summarization,
          and search. Runs on your infrastructure. No per-minute fees.
        </p>

        <div className={styles.heroDemo}>
          <img
            src={useBaseUrl('/img/opentranscribe-workflow.gif')}
            alt="OpenTranscribe interface showing transcription with speaker labels"
            loading="eager"
          />
        </div>

        <div className={styles.buttons}>
          <Link className={styles.primaryBtn} to="/docs/getting-started/quick-start">
            Get Started <IconArrowRight />
          </Link>
          <Link className={styles.secondaryBtn} to={githubRepo}>
            <IconGithub /> GitHub
          </Link>
        </div>

        <div className={styles.quickInstall}>
          <div>
            <div className={styles.installLabel}>Install with one command</div>
            <pre className={styles.installCommand}>
              <code>curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/setup-opentranscribe.sh | bash</code>
            </pre>
          </div>
        </div>
      </div>
    </header>
  );
}

function Stats() {
  const data = [
    {value: '70x', label: 'Realtime Speed'},
    {value: '100+', label: 'Languages'},
    {value: '8', label: 'LLM Providers'},
    {value: '$0', label: 'Per-Minute Cost'},
  ];

  return (
    <section className={styles.stats}>
      <div className="container">
        <div className="row">
          {data.map((s, i) => (
            <div className="col col--3" key={i}>
              <div className={styles.stat}>
                <div className={styles.statNumber}>{s.value}</div>
                <div className={styles.statLabel}>{s.label}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Features() {
  return (
    <section className={styles.features}>
      <div className="container">
        <Heading as="h2" className={styles.sectionTitle}>
          What you get
        </Heading>
        <p className={styles.sectionSubtitle}>
          A complete transcription platform, not just a Whisper wrapper.
        </p>
        <div className="row">
          {features.map((f, i) => (
            <div className="col col--4" key={i} style={{marginBottom: '1rem'}}>
              <div className={styles.featureCard}>
                <div className={styles.featureIcon}>{f.icon}</div>
                <div className={styles.featureTitle}>{f.title}</div>
                <p className={styles.featureDescription}>{f.desc}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function UseCases() {
  return (
    <section className={styles.useCases}>
      <div className="container">
        <Heading as="h2" className={styles.sectionTitle}>
          Use cases
        </Heading>
        <div className="row" style={{marginTop: '1.5rem'}}>
          {useCases.map((uc, i) => (
            <div className="col col--4" key={i} style={{marginBottom: '0.75rem'}}>
              <div className={styles.useCaseCard}>
                <div className={styles.useCaseIcon}>{uc.icon}</div>
                <div className={styles.useCaseTitle}>{uc.title}</div>
                <p className={styles.useCaseDesc}>{uc.desc}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Architecture() {
  const stack = [
    {name: 'WhisperX + PyAnnote v4', color: '#2563eb', desc: 'Transcription and speaker diarization'},
    {name: 'OpenSearch 3.4', color: '#475569', desc: 'Full-text and neural vector search'},
    {name: 'Celery + Redis', color: '#b45309', desc: '7-queue task pipeline (GPU/CPU split)'},
    {name: 'PostgreSQL', color: '#1d4ed8', desc: 'Relational storage with Alembic migrations'},
    {name: 'MinIO', color: '#7c3aed', desc: 'S3-compatible object storage, AES-256'},
    {name: 'Svelte + FastAPI', color: '#64748b', desc: 'Frontend and async API backend'},
  ];

  return (
    <section className={styles.architecture}>
      <div className="container">
        <div className={styles.archGrid}>
          <div className={styles.archText}>
            <Heading as="h3">Open stack, zero lock-in</Heading>
            <p>
              Every component is open source. Deploy on bare metal, in your cloud,
              or fully air-gapped. Same Docker Compose config scales from a laptop
              to a multi-GPU rack.
            </p>
            <ul className={styles.archList}>
              <li>3-stage Celery chain separates CPU and GPU work</li>
              <li>Multi-GPU worker scaling with configurable concurrency</li>
              <li>Automatic Alembic migrations on startup</li>
              <li>Non-root containers, health checks, Flower monitoring</li>
              <li>Offline deployment with pre-cached models</li>
            </ul>
          </div>
          <div className={styles.archVisual}>
            {stack.map((c, i) => (
              <div className={styles.archComponent} key={i}>
                <span className={styles.archDot} style={{background: c.color}} />
                <div>
                  <strong>{c.name}</strong>
                  <br />
                  <span style={{fontSize: '0.78rem', color: 'var(--ot-text-muted)'}}>{c.desc}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function Comparison() {
  const C = <Check />;
  const D = <Dash />;
  const S = (t: string) => <span style={{fontSize:'0.75rem'}}>{t}</span>;

  // Column indices: 0=OT, 1-5=commercial, 6-10=OSS
  const rows: {feature: string; values: ReactNode[]}[] = [
    {feature: 'Speaker diarization',            values: [C, C, C, C, C, C, C, C, C, D, S('Pro')]},
    {feature: 'Word-level timestamps',          values: [C, C, C, C, C, C, C, C, D, D, C]},
    {feature: '100+ languages',                 values: [C, D, C, S('20+'), S('49+'), S('50+'), C, C, C, C, C]},
    {feature: 'AI summarization',               values: [C, C, D, C, C, D, C, C, D, D, S('Pro')]},
    {feature: 'Custom LLM providers',           values: [C, D, D, D, D, D, C, C, D, D, D]},
    {feature: 'Full-text + semantic search',    values: [C, C, D, C, D, D, D, D, D, D, S('text')]},
    {feature: 'Cross-video speaker matching',   values: [C, D, D, D, D, D, D, D, D, D, D]},
    {feature: 'Self-hosted / air-gapped',       values: [C, D, D, D, D, D, C, C, C, C, D]},
    {feature: 'Enterprise auth (LDAP/PKI/OIDC)',values: [C, D, D, D, D, D, D, D, D, D, D]},
    {feature: 'Multi-user / roles',             values: [C, C, D, D, D, D, D, D, C, D, D]},
    {feature: 'Multi-GPU scaling',              values: [C, D, D, D, D, D, D, D, D, D, D]},
    {feature: 'Cloud ASR providers',            values: [C, D, D, D, D, D, D, D, D, D, D]},
    {feature: 'URL import (YouTube etc.)',       values: [C, D, D, D, D, D, C, D, D, D, S('Pro')]},
    {feature: 'Docker Compose deploy',          values: [C, D, D, D, D, D, D, C, C, C, D]},
    {feature: 'Desktop app',                    values: [D, D, D, D, D, D, C, D, D, D, C]},
    {feature: 'Subtitle editor',                values: [D, D, D, D, D, D, D, D, D, C, C]},
    {feature: 'SOC 2 / ISO 27001',             values: [D, D, C, C, C, C, D, D, D, D, D]},
  ];

  return (
    <section className={styles.comparison}>
      <div className="container">
        <Heading as="h2" className={styles.sectionTitle}>
          How it compares
        </Heading>
        <p className={styles.sectionSubtitle}>
          Feature comparison with commercial platforms and open source alternatives
        </p>
        <div className={styles.comparisonWrapper}>
          <table className={styles.comparisonTable}>
            <thead>
              <tr>
                <th rowSpan={2}>Feature</th>
                <th rowSpan={2} className={styles.highlightCol}>Open-<br/>Transcribe</th>
                <th colSpan={5} className={styles.groupHeader}>Commercial</th>
                <th colSpan={5} className={styles.groupHeader}>Open Source</th>
              </tr>
              <tr>
                <th>Otter.ai</th>
                <th>Rev</th>
                <th>Descript</th>
                <th>Sonix</th>
                <th>Trint</th>
                <th>Vibe</th>
                <th>Scriberr</th>
                <th>LinTO</th>
                <th>Whishper</th>
                <th>Mac-<br/>Whisper</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  <td>{r.feature}</td>
                  <td className={styles.highlightCol}>{r.values[0]}</td>
                  {r.values.slice(1).map((v, j) => (
                    <td key={j}>{v}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p style={{marginTop: '1.25rem', fontSize: '0.75rem', color: 'var(--ot-text-muted)', textAlign: 'center'}}>
          Data based on publicly available documentation as of March 2026. Features may vary by plan or version.
        </p>
      </div>
    </section>
  );
}

function CTA() {
  return (
    <section className={styles.cta}>
      <div className="container">
        <Heading as="h2" className={styles.ctaTitle}>
          Own your transcription pipeline
        </Heading>
        <p className={styles.ctaSubtitle}>
          Deploy in under 5 minutes. No account needed, no data leaves your servers.
        </p>
        <div className={styles.buttons}>
          <Link className={styles.primaryBtn} to="/docs/getting-started/quick-start">
            Install OpenTranscribe <IconArrowRight />
          </Link>
          <Link className={styles.secondaryBtn} to="/docs/getting-started/introduction">
            Read the Docs
          </Link>
        </div>
      </div>
    </section>
  );
}

/* ===== PAGE ===== */

export default function Home(): ReactNode {
  return (
    <Layout
      title="AI-Powered Transcription Platform"
      description="Self-hosted AI transcription with speaker identification, summarization, and search. Open source, privacy-first, production-ready.">
      <Hero />
      <main>
        <Stats />
        <Features />
        <UseCases />
        <Architecture />
        <Comparison />
        <CTA />
      </main>
    </Layout>
  );
}
