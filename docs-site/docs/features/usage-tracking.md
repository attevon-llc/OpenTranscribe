---
sidebar_position: 9
title: Usage Tracking
---

# Usage tracking

See how many tokens your AI features are consuming, broken down by model, with an
estimated cost.

This is available in **every** edition. If you run OpenTranscribe yourself and pay
an OpenAI, Anthropic or AWS bill, you have exactly the same question a hosted
customer does: where did it go?

## What is recorded

One record per assistant message, containing counts and identifiers only — **never
message content or transcript text**:

| Field | Meaning |
|---|---|
| Prompt / completion tokens | What the provider reported |
| Cache read / write tokens | Prompt-cache activity, tracked separately (see below) |
| Provider and model | Which model answered |
| Retrieved chunks | How many transcript excerpts were sent |
| Grounded | Whether the answer used your transcripts |

Records are keyed on the message, so a retry or a replay cannot double-count.

:::note[Usage history outlives conversations]
Usage records are stored separately from conversations on purpose. If you enable
chat retention, deleting old conversations does **not** erase your usage history —
which would otherwise destroy your own accounting the moment you turned retention
on.
:::

## Reading your usage

```
GET /usage/me?days=30          # totals + per-model breakdown
GET /usage/me/daily?days=30    # daily series, for charting
```

`days` accepts 1–365 and defaults to 30.

The summary returns per-model rows plus totals, including how many messages were
grounded in your transcripts versus answered without context.

## About the cost figures

**Costs are estimates, and they are labelled as such.** They come from a rate
table that a vendor can change at any time, and they ignore any negotiated
discount. The response includes `rates_verified_on` so you can see how fresh the
table is.

Three states are deliberately distinguished, because collapsing them would be
misleading:

| State | Meaning |
|---|---|
| **A cost** | The model has a known rate |
| **Free** | A local runtime (Ollama, vLLM) — you already paid for the hardware |
| **Unpriced** | No known rate; tokens are reported, cost is left blank |

When any model in the window is unpriced, the response sets `cost_incomplete` so a
total is never presented as complete when it isn't. A confident `$0.00` is a worse
answer than an honest blank.

:::note[Amazon Bedrock reports tokens only]
Bedrock is operated by AWS with its own rate card, separate from Anthropic's
first-party pricing. Estimating Bedrock spend from Anthropic's published rates
would produce a confidently wrong number, so OpenTranscribe reports Bedrock usage
in tokens and leaves the cost blank. See
[AWS Bedrock pricing](https://aws.amazon.com/bedrock/pricing/).
:::

### Prompt-cache tokens

Cache reads and writes are tracked and priced **separately** from ordinary input
tokens, because they don't cost the same: a cache read bills far below the
uncached input rate, and a cache write bills above it. Folding either into the
input count would misprice every cache-enabled deployment — in opposite
directions.

## What drives chat cost

The number that surprises people is the **retrieved context**, not the question.

A chat message sends the retrieved transcript excerpts plus recent conversation
history, so a five-word question can still carry several thousand input tokens.
At the default of ~12 excerpts of ~200 words each, that is roughly 3,000–4,000
tokens on *every* message regardless of what you typed.

To reduce it:

- **Scope tightly.** Fewer recordings means fewer, better excerpts.
- **Start a new chat when the topic changes** — long threads carry history forward.
- **Lower "Excerpts per answer"** in **Settings → Chat & RAG** (administrators).
- **Turn off "Use my transcripts"** for questions that aren't about your
  recordings; no retrieval happens, so only your question is sent.

See [AI Chat](./rag-chat.md#chatting-efficiently) for the fuller version.

## Limits

Administrators can cap per-user messages per hour and concurrent replies in
**Settings → Chat & RAG**. These bound request *volume*; the excerpt settings
above bound the tokens each request costs.
