---
sidebar_position: 7
title: AI Chat (RAG)
---

# AI Chat

Ask questions across your recordings and get answers grounded in what was actually
said, with citations that link straight to the moment in the transcript.

Chat is a first-class page in the navbar. It uses **Retrieval Augmented
Generation (RAG)**: rather than sending whole transcripts to a language model,
OpenTranscribe searches your library for the passages most relevant to your
question and gives the model only those, along with instructions to cite them.

## What it is good for

The feature earns its keep on questions that would otherwise mean re-listening:

- *"What action items came out of these three calls?"*
- *"What did the customer say about pricing, and how did we respond?"*
- *"Summarise the decisions and who made them."*
- *"Did anyone raise concerns about the timeline?"*

Because retrieval spans multiple recordings, it is particularly useful for
questions **across** a set of meetings — trends, recurring objections, follow-ups
that were promised and never closed.

## Choosing what to chat about

The bar above the composer always shows what the conversation is grounded in.
By default it is **All transcripts** — everything you can access.

To narrow it, use **Add context**, which offers four ways to select:

| Tab | Use when |
|---|---|
| **Recordings** | You know the specific files. |
| **Collections** | You want a whole project or client. Membership is resolved at query time, so recordings added to the collection later are automatically in scope. |
| **Tags** | You organise by topic or status rather than by folder. |
| **Speakers** | You care about what specific people said. |

### Asking about one person

The **Speakers** tab is the filter with no equivalent in a general-purpose
document chat, and it is exact rather than approximate. Transcripts are indexed
as *speaker turns*, so one passage is one person talking — selecting a speaker
retrieves only their words.

That difference matters. Without it, "what did Dana commit to?" can be answered
from a sentence in which someone *else* mentions Dana. With it, the answer can
only come from Dana's own turns.

Speakers are a separate axis from the other three: recordings, collections and
tags choose *which* recordings to search, speakers choose *who* to listen to
within them. Use them together ("what did Dana say in the Q3 calls?") or on
their own ("everything Dana said, anywhere"). The assistant is told about an
active speaker filter, so it says a person is out of scope rather than claiming
they were never discussed.

You can also start from the gallery: select recordings and choose **Chat with N**
from the bulk actions menu.

:::tip Narrower is usually better
Retrieval quality drops when a question has to compete with an entire library.
If you know which meetings matter, select them — answers get noticeably more
specific.
:::

The footer of the picker estimates how much of the model's context window your
selection would occupy, and warns before you pick more than the model can
usefully handle.

## Citations

Every answer that uses your transcripts cites them with numbered markers like
`[1]`, and lists the sources underneath. Each source card shows the recording,
the speaker, and the timestamp; clicking it opens that recording **at that
moment** in the player.

Citations are built from the retrieval results, never parsed out of the model's
prose, so a citation always points where it claims to point.

If the excerpts do not contain the answer, the assistant is instructed to say so
rather than guess.

## Working with a conversation

| Action | Where |
|---|---|
| **Edit a question** | Hover a question → pencil. The answer is regenerated from that point; later turns are retired, not deleted. |
| **Regenerate** | Hover the latest answer → circular arrow. |
| **Stop generating** | The send button becomes Stop while streaming, or press `Escape`. |
| **Copy** | Hover any message → copy. Code blocks get their own copy button. |
| **Export** | Download icon in the chat header — Markdown or JSON, with sources as deep links. |
| **Rename / archive / delete** | Hover a conversation in the sidebar. Archived chats stay available under *Show archived*. |

Keyboard shortcuts follow the usual conventions: `Cmd/Ctrl+Shift+O` starts a new
chat, `Cmd/Ctrl+/` focuses the composer, and `Escape` stops generation.

## Chat controls

The gear icon in the chat header opens per-conversation settings:

- **Use my transcripts** — turn context off to use the model as a plain
  assistant. The context bar shows an unmistakable *Context off* chip so an
  ungrounded answer never looks like a grounded one.
- **Instructions for this chat** — extra guidance for this conversation only
  (for example, *"answer as a concise meeting summary, action items last"*).
- **Creativity** — lower values stay closer to what was actually said.
- **Retrieval mode** — three genuinely different searches:
  - *Hybrid* (default) — keyword and meaning together, fused. Right almost always.
  - *Meaning* — vector search only. Finds passages that say the same thing in
    different words; may miss a rare literal term like a product code.
  - *Exact words* — keyword only. Use when you know the phrasing.
- **Model** — pin a different AI provider or model for this conversation only.
  Switching to one with a smaller context window warns first, since it silently
  changes how much of the conversation the model can see.

Account-wide defaults for new conversations live in **Settings → Chat**. Any
individual conversation can override them.

## Requirements

Chat needs a language model. Configure a provider in **Settings → AI** — OpenAI,
Anthropic, OpenRouter, or a self-hosted vLLM / Ollama endpoint. Until one is
configured, the chat page shows a setup prompt instead of a composer.

Retrieval uses the same OpenSearch index that powers search, so recordings must
have finished transcribing to be searchable.

## Privacy and safety

- **Redaction is honoured.** If you have *redact before LLM* enabled (or an
  administrator enforces it), retrieved excerpts are re-masked before they reach
  the provider, and the stored answer and citation snippets keep that masking.
  Masking fails closed: if a passage cannot be masked, it is withheld rather than
  sent.
- **Your conversations are private.** They are never shared with other users,
  and they are removed by GDPR erasure along with the rest of your data.
- **Transcript content is treated as data, not instructions.** Excerpts are
  delimited and the model is told explicitly never to follow directions found
  inside them, so a recording cannot hijack the assistant.
- **Answers are rendered safely.** Model output is sanitised, and links it writes
  can never point back into the application.

## Administration

**Settings → Chat & RAG** (administrators) tunes retrieval and limits. Every
value applies to the next message — no restart, no `.env` edit:

| Setting | Effect |
|---|---|
| Candidate excerpts | How many passages are retrieved before reranking. Higher finds more, costs time. |
| Excerpts per answer | How many reach the model. |
| Max excerpts per recording | Stops one long recording dominating a multi-file conversation. |
| Rerank excerpts | A CPU cross-encoder that re-scores passages for relevance. Improves precision; adds latency and about 350–500&nbsp;MB of memory on first use. |
| Rewrite follow-up questions | Expands pronouns and references ("what about her?") before searching. |
| Retrieval cache | Short-lived reuse of identical searches. |
| Messages per hour / concurrent replies | Per-user abuse controls. |
| Delete conversations after | Optional retention window (0 keeps them indefinitely). |

The reranker model (`cross-encoder/ms-marco-MiniLM-L-6-v2`, ~90&nbsp;MB) is
downloaded by `scripts/download-models.py`. If it is absent, reranking disables
itself with a warning and chat continues to work.

## How it works

```
Your question
  → rewritten into a standalone query (if it is a follow-up)
  → hybrid search over transcript chunks (keyword + meaning, RRF-fused)
  → reranked by a cross-encoder for relevance
  → sampled across files so no single recording dominates
  → re-masked under your redaction policy
  → sent to your model with citation instructions
  → streamed back token by token
```

Transcripts are indexed as **speaker-turn chunks**, so a retrieved passage is a
coherent stretch of one person talking rather than an arbitrary word window —
which is what makes speaker attribution in answers reliable.
