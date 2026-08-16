---
sidebar_position: 10
title: Working Without an AI Model
description: What OpenTranscribe does when no LLM provider is configured — and what needs one
---

# Working Without an AI Model

OpenTranscribe runs perfectly well with **no language model configured at all**. That is a
supported deployment, not a degraded one: transcription, diarization, speaker matching and the
full search stack — including semantic search — are local models that never call a provider.

What needs an LLM is the small set of features that *write prose*: summaries, topic suggestions,
and chat answers.

## The short version

| You want to | Needs an AI provider? |
|---|---|
| Transcribe audio and video | No |
| Separate and identify speakers | No |
| Match a speaker across recordings by voice | No |
| Keyword search | No |
| **Semantic / hybrid search** | **No** |
| Redact PII, profanity or toxic content | No |
| Tag, collect, share, export, subtitle | No |
| Watch folders / S3 / SMB for new media | No |
| Speaker and meeting analytics | No |
| Generate a summary | Yes |
| Suggest topics, tags or collections | Yes |
| Suggest who a speaker is from what they said | Yes |
| **Ask a question in Chat** | **Yes** |

Leave `LLM_PROVIDER` empty (or simply never configure a provider in **Settings → AI**) and
everything in the first group works normally. Nothing is disabled, hidden behind a paywall, or
degraded — the features that need a model tell you so, and the rest do not mention it.

## Semantic search does not need a provider

This is the part people expect to lose, and it is worth being precise about.

OpenTranscribe's hybrid search combines keyword matching with **vector search over an embedding
model that runs inside your own OpenSearch container** (`all-MiniLM-L6-v2` by default, about
80&nbsp;MB, downloaded once). An embedding model is not a language model: it turns text into
coordinates so that "budget concerns" can find "cost overruns", and it neither generates text nor
talks to anyone.

So on a stack with no provider configured you still get:

- **Meaning-based search** — paraphrases, synonyms, related phrasing.
- **Hybrid ranking** — keyword and semantic results fused into one list.
- **Speaker, date, tag, collection and duration filters** on top of either.

See [Search & Filters](./search-and-filters.md). If your deployment has no internet access at all,
pre-fetch the model as described in
[Offline Installation](../installation/offline-installation.md).

:::tip Search is the honest answer to most "what did we say about X" questions
Chat is more convenient, but search over the same index gets you to the same passage, and it
never invents anything. On a no-LLM deployment it is the primary way through your library, not a
consolation prize.
:::

## Chat is the one feature that genuinely stops

Without a provider the **Chat** page shows a setup prompt — *"Connect an AI provider to start
chatting"* — instead of a composer. There is no fallback mode: retrieval can find the right
passages, but nothing can write an answer about them.

Everything *around* the answer is local. Retrieval, reranking, redaction masking and citation
building all run in your stack; the only thing that leaves it is the prompt sent to whichever
provider you chose. If you self-host vLLM or Ollama, nothing leaves your network at all — see
[LLM Integration](../features/llm-integration.md).

### Trying chat without paying for a model

For evaluation and development there is a canned OpenAI-compatible mock:

```bash
./opentr.sh start dev --with-mock-llm
```

Only token generation is faked. Retrieval, redaction, citations and streaming take their real
code paths, so it is a legitimate way to see how the feature behaves — but it is a development
tool, not a deployment mode. Details in
[RAG Chat (Internals)](../developer-guide/rag-chat.md#local-development-without-a-gpu-or-api-key).

## Turning a model on later

Adding a provider is retroactive. Configure one in **Settings → AI** and every recording you
already have becomes summarizable and chattable immediately — there is no re-processing step,
because summaries and chat read the transcripts and the search index that already exist.

The reverse is also true: removing the provider stops those features and leaves everything else
untouched.

:::note Planned: more of this list moves to the "no provider needed" column
Work under
[issue&nbsp;#403](https://github.com/attevon-llc/OpenTranscribe/issues/403) adds deterministic
per-file facts (duration, speaker roster, talk-time), extractive digests, and a composed
collection overview — all computed without a model, so a no-LLM deployment gets a written
overview of a collection rather than only a file list. **None of it has shipped yet**; this note
exists so the intent is on record, not to describe something you can use today.
:::

## Related

- [LLM Integration](../features/llm-integration.md) — configuring a provider when you want one
- [Chatting with Your Transcripts](./chatting-with-transcripts.md)
- [Search & Filters](./search-and-filters.md)
- [FAQ: do I need an LLM?](../faq.md)
