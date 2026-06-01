# Cloud ASR Provider Market Research

*Generated May 2026. Pricing, headcount, and funding figures change frequently — treat specific numbers as approximate benchmarks rather than live rates.*

---

## Deepgram

**Core product:** Speech-to-text (STT) and text-to-speech (TTS) APIs with a focus on low-latency, real-time conversational applications. Key models include Nova-3 (batch/streaming STT), Aura-2 (TTS, sub-200 ms latency), and a Voice Agent API with interruption handling and turn detection. Targets developers building voice AI products rather than end-users.

**Pricing:** Pay-as-you-go for small volumes. **Growth tier** starts at $4,000+/year with up to 20% pre-pay discounts. **Enterprise tier** starts at $15,000+/year and adds custom model fine-tuning, dedicated support, and volume commitments. A $200 free credit is offered to new accounts.

**Key differentiators:**
- Purpose-built neural architecture (not a wrapped Whisper product) with claimed latency advantages for streaming.
- Nova-3 is competitive on accuracy across general English; Voice Agent API abstracts turn-taking complexity.
- Actively expanding into TTS and full voice agent stack — positioning as a one-stop voice AI platform.

**Company profile:**
- Founded: 2015. HQ: San Francisco, CA.
- Funding: Over $215 million total; most recent round was a $130 million Series C in January 2026 at a $1.3 billion valuation. Twilio participated as a strategic investor in the Series C.
- Customers: 1,300+ organizations, including Granola, Vapi, and Twilio.
- Employee count: ~200–300 (estimated; not publicly disclosed).

**Business model:** Pure API — metered usage with tiered annual commitment discounts. No end-user product.

**Positioning:** Developer-first. Heavy adoption in voice agent / conversational AI builders, contact center automation, and real-time transcription tooling.

---

## AssemblyAI

**Core product:** Developer API for speech-to-text, audio intelligence, and real-time streaming transcription. Goes beyond raw transcription with built-in audio intelligence features: speaker diarization, sentiment analysis, entity detection, PII redaction, chapter detection, and an LLM-powered "LeMUR" layer for Q&A over transcripts. Their Universal-1 model targets high accuracy across English and multiple languages.

**Pricing:** Pay-as-you-go model. Approximately $0.21/hour for batch STT and $0.45/hour for streaming (indicative — verify current rates). Speaker diarization is included at no extra cost. Free tier available for development/testing. Enterprise pricing is negotiated.

**Key differentiators:**
- Richest out-of-the-box audio intelligence layer of any specialist provider: diarization, sentiment, entities, PII redaction, and LLM Q&A (LeMUR) bundled into a single API.
- Strong developer experience and documentation.
- LeMUR (large language model for universal retrieval) adds a RAG-like layer on top of transcripts, enabling chat and Q&A against audio content.

**Company profile:**
- Founded: 2017. HQ: San Francisco, CA.
- Funding: Raised a Series B ($115 million) in 2023; total funding reportedly above $115 million. Exact ARR not publicly confirmed (third-party estimates vary widely and were not verified).
- Employee count: ~200–350 (estimated).

**Business model:** Metered API with developer self-serve and enterprise contracts. No end-user product.

**Positioning:** Developer-first, broad horizontal use cases. Strong in podcast/media transcription, customer support analytics, and any workflow requiring audio intelligence beyond raw text.

---

## OpenAI (Whisper API)

**Core product:** Cloud-hosted Whisper model exposed as a REST API via the OpenAI platform (`/v1/audio/transcriptions`, `/v1/audio/translations`). Supports 57+ languages. Provides transcription and translation to English. No streaming; batch only.

**Pricing:** Flat per-minute rate: approximately $0.006/minute ($0.36/hour) for the `whisper-1` model. No free tier for production use; new accounts get a limited API credit. No enterprise model-level variant — same model for all tiers. Volume discounts via enterprise agreements.

**Key differentiators:**
- Low engineering overhead: same key as GPT/embeddings, no separate account. Ideal for teams already on the OpenAI platform.
- Translation to English is built-in (not available on turbo-class Whisper).
- Not a streaming API — limited to pre-recorded audio files up to 25 MB.
- No speaker diarization, no audio intelligence features.

**Company profile:**
- Founded: 2015. HQ: San Francisco, CA.
- Funding: Over $17 billion raised (including Microsoft's multi-year commitment). Valued at ~$157 billion+ (as of early 2026). Employees: 3,000+.
- Speech API is one product line within a much larger platform (GPT, DALL·E, embeddings, etc.).

**Business model:** Platform API with metered pay-per-use and ChatGPT Plus/Team/Enterprise subscriptions. Speech is a secondary revenue line; the primary business is LLM API access.

**Positioning:** Convenience play for OpenAI platform users. Not specialized for speech; no streaming, no diarization. Adequate for simple batch transcription when you're already in the OpenAI ecosystem.

---

## Google Cloud Speech-to-Text

**Core product:** Google's cloud-hosted STT API (v1/v2 and Chirp model). Supports 125+ languages. Features include streaming, batch, phone call audio, medical transcription (a separate product), and word-level confidence scores. Chirp is their most recent large ASR model.

**Pricing:** Tiered pay-per-use. Standard model: approximately $0.006–$0.016/minute depending on features (diarization, data logging opt-out). Free tier: 60 minutes/month. Enhanced/Chirp models priced higher. Prices can add up quickly when diarization or phone models are enabled.

**Key differentiators:**
- Breadth of language support (125+ languages/dialects) — best-in-class for low-resource languages.
- Deep integration with Google Cloud ecosystem (Pub/Sub, GCS, Vertex AI).
- Chirp model is competitive on multilingual accuracy.
- Medical Speech-to-Text for healthcare verticals.

**Company profile:**
- Founded: Google LLC, 1998 (Alphabet subsidiary). HQ: Mountain View, CA.
- Revenue: Alphabet generated ~$350 billion in 2025 total revenue. Cloud segment (including Speech API) ~$43 billion; Speech is a small fraction.
- Employees: ~180,000 total.

**Business model:** Cloud platform — Speech API is a line item in GCP, bundled with enterprise committed-use agreements. No standalone speech product.

**Positioning:** Enterprises already on GCP, multilingual global deployments, government/regulated verticals. Less developer-friendly DX than Deepgram or AssemblyAI, but unmatched language breadth.

---

## Azure AI Speech (Microsoft)

**Core product:** Microsoft's speech platform, part of Azure Cognitive Services. Includes STT, TTS, speaker recognition, pronunciation assessment, and real-time/batch modes. Custom Speech allows fine-tuning on domain-specific vocabulary. Supports 100+ languages.

**Pricing:** Free tier: 5 hours/month for standard, 1 hour/month for custom. Standard: approximately $0.016/minute ($1/hour) for standard STT; custom speech: $1.40/hour for recognition, plus $10/hour training. Pronunciation assessment, diarization, and other features billed separately.

**Key differentiators:**
- Custom Speech fine-tuning for domain-specific vocabulary (medical, legal, technical) without retraining from scratch.
- Pronunciation Assessment — unique feature for language learning apps.
- Deep Azure integration: Active Directory, compliance certifications (FedRAMP, HIPAA, ISO 27001).
- Fast Transcription API for batch at scale.

**Company profile:**
- Founded: Microsoft, 1975. HQ: Redmond, WA.
- Revenue: Microsoft generated ~$246 billion in FY2025 revenue. Azure Cognitive Services is a small slice of the Azure cloud segment (~$135 billion).
- Employees: ~220,000 total.

**Business model:** Cloud platform — Speech is part of Azure, sold through enterprise agreements and PAYG. Strong government/FedRAMP channel.

**Positioning:** Microsoft-ecosystem enterprises, regulated industries requiring compliance certifications, language learning applications (pronunciation assessment), and organizations that need Custom Speech for specialized vocabulary.

---

## AWS Transcribe (Amazon)

**Core product:** Amazon's managed STT service, part of AWS. Supports batch and streaming transcription, 100+ languages, custom vocabulary, custom language models, speaker diarization, medical transcription (Transcribe Medical), and Call Analytics for contact centers. PII redaction and content redaction built in.

**Pricing:** Free tier: 60 minutes/month for 12 months. Standard: approximately $0.024/minute ($1.44/hour) for general STT — significantly higher than specialized providers. Custom language models and medical transcription cost more. Costs scale with add-ons (diarization, analytics).

**Key differentiators:**
- Deepest AWS ecosystem integration: S3, Lambda, Kinesis, Contact Lens, Connect — fits naturally into AWS-native architectures.
- Transcribe Medical is HIPAA-eligible with specialized medical vocabulary.
- Call Analytics for contact centers: sentiment, interruptions, talk time, issue categories.
- Not competitive on pure price/performance vs. Deepgram or AssemblyAI.

**Company profile:**
- Founded: Amazon, 1994. HQ: Seattle, WA.
- Revenue: AWS generated ~$108 billion in 2024 revenue. Transcribe is a small line item.
- Employees: ~1.5 million total (Amazon).

**Business model:** Cloud platform — Transcribe is an AWS service billed as part of an AWS account. Sold through AWS enterprise agreements and Marketplace.

**Positioning:** AWS-native organizations, contact center analytics (via Connect + Contact Lens), healthcare (Transcribe Medical). Not competitive for developers who want best-in-class accuracy at low cost. Slowest provider in benchmarks due to S3-batch pipeline.

---

## Speechmatics

**Core product:** Speech recognition API and on-premises/private cloud deployment option. Supports 50+ languages with strong accuracy claims across accents and non-native speakers. Offers real-time and batch modes. Notable for on-prem deployment — unique among specialist providers. Features include diarization, custom dictionary, and punctuation/formatting.

**Pricing:** Pay-as-you-go and subscription tiers. Real-time and batch rates quoted on request; generally positioned at a premium vs. Deepgram/AssemblyAI. Enterprise and on-prem licensing negotiated. A free tier/trial is available.

**Key differentiators:**
- On-premises and private cloud deployment (BYOC) — critical for air-gapped, regulated, or high-privacy deployments.
- Claimed strong accent/dialect robustness, particularly for British English and international accents.
- Not consumer-facing; API-only with on-prem as a genuine differentiator.

**Company profile:**
- Founded: 2006 (spun out of Cambridge University research). HQ: Cambridge, UK.
- Funding: Total funding reported in various ranges (~$60–90M); a significant Series B was raised in 2022 (exact figures disputed in verification — treat as approximate). Backed by Susquehanna Growth Equity and others.
- Employees: ~200–300 (estimated).

**Business model:** API-as-a-service plus on-premises licensing. On-prem licenses command higher contract values. Strong enterprise sales motion.

**Positioning:** Enterprise and regulated industries requiring on-prem deployment, UK/European market, government and defense, any use case where audio cannot leave the customer's infrastructure.

---

## Gladia

**Core product:** Audio transcription and real-time speech recognition API, built on top of (and enhancing) Whisper with additional post-processing for accuracy, real-time quality improvements, speaker diarization, and translation. Focus on achieving batch-level quality at real-time speed — the company's stated core differentiation. Supports 100+ languages.

**Pricing:** Pay-as-you-go starting at approximately $0.60/audio-hour (rate was unverified in adversarial review — treat as indicative). Enterprise pricing negotiated. Free trial available.

**Key differentiators:**
- CEO Jean-Louis Quéguiner explicitly positioned the company around fixing the accuracy gap in real-time transcription: *"real time wasn't very good in terms of quality in the market in general"* — Gladia's goal is batch quality with real-time speed.
- Multilingual support with translation baked in.
- Smaller/more agile than the cloud incumbents; competes head-on with AssemblyAI, Deepgram, and Speechmatics per the company's own framing.

**Company profile:**
- Founded: 2022. HQ: Paris, France.
- Funding: ~$16 million Series A (October 2024, led by XAnge) following an earlier seed round. Total raised approximately $20 million.
- Employees: ~30 as of October 2024, with planned hiring.
- Named direct competitors per company: AssemblyAI, Deepgram, Speechmatics, and the three cloud incumbents (Amazon, Microsoft, Google).

**Business model:** Pure API, metered usage with enterprise contracts.

**Positioning:** Developer-focused European alternative to US-based specialist providers. Strong multilingual focus suits global media and international enterprise use cases. Smallest team of any provider in this report — high execution risk but also most agile.

---

## pyannote.ai

**Core product:** "Speaker Intelligence Platform" — an API focused not on transcription per se but on speaker diarization, speaker identification, voice biometrics, and speaker attribution across languages and acoustic conditions. Built on top of the open-source pyannote-audio research library. Positions as the speaker intelligence layer of the Voice AI stack, complementing transcription APIs rather than replacing them.

**Key differentiators:**
- The only specialist provider in this list focused exclusively on speakers (who said what) rather than transcription (what was said). Directly complements any STT API.
- Voice fingerprinting and cross-recording speaker matching.
- Built by the creators of the most widely-used open-source diarization research library (pyannote-audio), giving deep academic credibility.
- "Speaker Intelligence" brand is unique — no direct competitor occupies this positioning.

**Pricing:** Not publicly listed at time of research. API access via paid plans; pricing likely consumption-based per audio hour processed. Contact for enterprise terms.

**Company profile:**
- Founded: 2024. HQ: Paris, France.
- Funding: $9 million seed round (April 2025), led by Crane Venture Partners and Serena. Angel investors include Julien Chaumond (HuggingFace CTO) and Alexis Conneau (formerly Meta/OpenAI, co-founder of WaveForms AI).
- Founders: Hervé Bredin (Co-founder & CSO, former CNRS research scientist and pyannote-audio creator), Vincent Molina (CEO), Juan Coria (CTO).
- Employees: ~15–30 (early-stage; not publicly disclosed).

**Business model:** API-as-a-service. Revenue from developer consumption and enterprise contracts for speaker intelligence features.

**Positioning:** Developers and enterprises that need accurate speaker attribution layered on top of their existing transcription pipeline. Media, contact center, legal/compliance, and any multi-speaker meeting intelligence workflow. Earliest stage company in this report — still building out the product surface.

---

## Market Landscape Synthesis

The cloud ASR market in 2026 is a two-tier structure: cloud platform incumbents (AWS, Google, Azure) offer speech as a bundled service within vast cloud ecosystems, competing primarily on integration convenience, compliance certifications, and enterprise relationships rather than accuracy or price. The specialist providers (Deepgram, AssemblyAI, Speechmatics, Gladia) compete aggressively on model quality, latency, and developer experience, typically offering better accuracy per dollar than the incumbents. Every provider in this list — from pyannote.ai's 30-person Paris startup to AWS's trillion-dollar parent — operates purely as an API or infrastructure layer. None of them ships a polished, user-facing transcription application that a non-developer could pick up and use to transcribe a meeting, label speakers, search recordings, or summarize conversations. This is the gap that OpenTranscribe fills: it sits on top of this API layer (using any of these providers as a pluggable backend) and delivers a complete end-user product — upload, transcribe, diarize, search, summarize, manage — without requiring users to write a line of code or understand the difference between Nova-3 and Chirp. In a market where every competitor is a developer tool, an accessible, self-hosted, multi-provider application is a structurally differentiated product.

---

*Sources: TechCrunch, Sifted, TechFundingNews, Crane Venture Partners, company pricing pages and press releases. All pricing figures indicative — verify current rates before making procurement decisions.*
