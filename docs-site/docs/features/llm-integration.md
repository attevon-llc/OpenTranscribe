---
sidebar_position: 1
---

# LLM Integration

OpenTranscribe integrates with multiple Large Language Model (LLM) providers for AI-powered features like summarization and speaker identification.

## Supported Providers

- **vLLM**: Self-hosted, high-performance inference
- **OpenAI**: GPT-4, GPT-4o, and compatible models
- **Anthropic**: Claude models via the first-party API
- **Ollama**: Local LLM server with many model options
- **OpenRouter**: Access to multiple models through one API
- **Amazon Bedrock**: AWS-native access to Claude, Nova, Llama and Mistral — **no API key required**

## Key Features

### AI Summarization

Generate BLUF (Bottom Line Up Front) summaries with:
- Executive summary
- Key discussion points
- Speaker analysis and talk time
- Action items with priorities
- Decisions and follow-ups

**Multilingual Output (New in v0.2.0)**:
Generate summaries in 12 different languages:
- English, Spanish, French, German
- Portuguese, Chinese, Japanese, Korean
- Italian, Russian, Arabic, Hindi

Configure in Settings → Transcription → LLM Output Language.

### Organization Context (New in v0.4.0)

Inject organization-specific context into AI prompts for more relevant summaries:

- Define organization context text (e.g., team names, project acronyms, domain terminology)
- Context is automatically included in summarization and speaker identification prompts
- Configurable per-user in Settings → AI → Organization Context
- Toggle whether context applies to default prompts, custom prompts, or both

The organization context is injected as a system-level preamble before the transcript content in all LLM calls. This allows the model to correctly resolve ambiguous references -- for example, knowing that "the Board" refers to a specific governance body, or that "Q3" means a particular fiscal quarter for your organization. The context text is stored per-user, so different teams can define their own terminology without conflicting.

### Per-Collection AI Prompts (New in v0.4.0)

Collections can have a default summarization prompt:

- Assign a custom summary prompt to any collection
- Files in that collection automatically use the collection's prompt when summarized
- Useful for standardizing output format across related files (e.g., all meeting notes use the same template)
- Configure via the collection settings or prompt management UI

Prompt inheritance follows a clear priority chain: per-file custom prompt > collection default prompt > user default prompt > system default prompt. When a file belongs to multiple collections, the most recently assigned collection's prompt takes precedence.

### Auto-Label (New in v0.4.0)

AI-powered automatic tagging and collection assignment ([#140](https://github.com/attevon-llc/OpenTranscribe/issues/140)):

- After transcription, the LLM suggests topic tags based on content analysis
- High-confidence suggestions (>= configurable threshold, default 0.75) are automatically applied as tags
- Related files in a batch upload are grouped into collections by shared topics
- Configurable confidence threshold for auto-application
- Enable/disable separately for tags and collections
- Retroactive auto-labeling available for existing files
- Configure in Settings → AI → Auto-Label

#### How the Auto-Label Pipeline Works

1. **Topic extraction**: After transcription completes, the LLM analyzes the transcript and produces tag and collection suggestions with confidence scores.
2. **Fuzzy deduplication**: Before creating new tags, the system normalizes names (lowercasing, whitespace/hyphen normalization) and runs `difflib.SequenceMatcher` (no extra dependencies) with a 0.85 similarity threshold to match against existing tags. This prevents near-duplicates like "machine-learning", "machine learning", and "Machine Learning" from coexisting.
3. **Auto-apply**: Suggestions at or above the confidence threshold are applied automatically. Below-threshold suggestions remain available for manual review in the UI.
4. **Batch grouping**: For bulk imports, the system tracks which files were uploaded together via an `upload_batch` table. After all files in a batch complete topic extraction, topics appearing in 2+ files trigger automatic shared collection creation.
5. **Provenance tracking**: Every tag and collection tracks its `source` ("manual", "auto_ai", or "bulk_group") so users can distinguish AI-applied labels from human ones. The frontend displays a sparkle icon on auto-applied items with confidence tooltips.

### Disable AI Summary Generation

Users can disable automatic AI summarization:

- **Per-upload**: Toggle "Generate AI Summary" off in the upload dialog to skip summarization for a specific file
- **User default**: Set your default in Settings → AI → Auto-Summarize to prevent automatic summarization on all uploads
- Disabling auto-summarize does not prevent manual summarization — users can still click "Generate Summary" on any transcript at any time

### Prompt Sharing

Custom AI prompts can be shared between users:

- Users can share their custom summarization prompts with other users or groups via the sharing system
- Shared prompts appear in the recipient's prompt selection dropdown alongside their own prompts
- Sharing is managed from Settings → AI Prompts → Share
- Useful for standardizing summarization output across a team without each member creating identical prompts

### Speaker Identification

LLM-powered speaker name suggestions based on:
- Conversation context
- Speaking patterns
- Topic expertise
- Cross-video speaker matching

### Model Auto-Discovery (New in v0.2.0)

Automatic model discovery for multiple providers:

**Supported providers:**
- **vLLM**: OpenAI-compatible /v1/models endpoint
- **Ollama**: Native /api/tags endpoint
- **Anthropic**: Native /v1/models endpoint

**Features:**
- Model selection dropdown populated dynamically
- No manual model name entry required
- Edit mode supports stored API keys (no need to re-enter)
- Works with any OpenAI-compatible API endpoint

## Configuration

Set your preferred provider in `.env`:

```bash
# LLM Provider Selection
LLM_PROVIDER=vllm  # or: openai, anthropic, ollama, openrouter

# Provider-specific settings
VLLM_API_URL=http://your-vllm-server:8000/v1
OPENAI_API_KEY=sk-xxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxx
OLLAMA_API_URL=http://localhost:11434
```

## Provider-Specific Guides

### vLLM (Self-Hosted)

Best for privacy-first deployments:

```bash
# Example vLLM server setup
docker run --gpus all -p 8000:8000 vllm/vllm-openai:latest \
  --model meta-llama/Llama-2-70b-chat-hf
```

Configure in `.env`:
```bash
LLM_PROVIDER=vllm
VLLM_API_URL=http://localhost:8000/v1
VLLM_MODEL_NAME=meta-llama/Llama-2-70b-chat-hf
```

### OpenAI

Quick setup with commercial API:

```bash
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-xxxxx
OPENAI_MODEL=gpt-4o
```

### Amazon Bedrock

The AWS-native option. Unlike every other provider, Bedrock needs **no API key**:
boto3 resolves credentials through the standard AWS chain — instance role, task
role, shared profile, or environment — so a deployment on EC2, ECS or EKS
provisions no secret at all.

```bash
LLM_PROVIDER=bedrock
BEDROCK_REGION=us-east-1              # falls back to AWS_REGION / AWS_DEFAULT_REGION
BEDROCK_MODEL_NAME=anthropic.claude-haiku-4-5-20251001-v1:0
```

Required IAM permissions:

| Action | Needed for |
|---|---|
| `bedrock:InvokeModelWithResponseStream` | Chat (streaming) |
| `bedrock:InvokeModel` | Summaries, topics, speaker ID (non-streaming) |

**Cross-region inference profiles are applied automatically.** A bare
foundation-model ID only works where that model is provisioned in your exact
region; OpenTranscribe prefixes it with the geography derived from
`BEDROCK_REGION` (`us.`, `eu.`, `apac.`), which lets AWS route around a saturated
home region — the difference between a throttle and a served request at peak.

To pin an exact profile instead, set a fully-qualified ID or an ARN and it is used
verbatim:

```bash
# Already prefixed — used as-is
BEDROCK_MODEL_NAME=us.anthropic.claude-haiku-4-5-20251001-v1:0

# An application inference profile (e.g. one carrying cost-allocation tags)
BEDROCK_MODEL_NAME=arn:aws:bedrock:us-east-1:123456789012:inference-profile/my-profile
```

:::tip[Verify the model ID against your account]
AWS rotates model IDs, and access is per-account. Run
`aws bedrock list-inference-profiles --region <your-region>` to see exactly what
you can invoke, rather than copying an ID from documentation.
:::

**Cost attribution.** Every request carries `requestMetadata` identifying the
user, organization and conversation, so Bedrock's own invocation logs can be
reconciled against OpenTranscribe's usage records.

:::note[Bedrock pricing is separate]
Bedrock is operated by AWS with its own rate card, not Anthropic's. OpenTranscribe
therefore reports Bedrock usage in **tokens only** and does not estimate a dollar
cost for it — a confident wrong number would be worse than none. See
[AWS Bedrock pricing](https://aws.amazon.com/bedrock/pricing/).
:::

### Anthropic

Claude models with automatic model discovery:

```bash
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-xxxxx
ANTHROPIC_MODEL=claude-opus-4-5-20251101  # or claude-sonnet-4-20250514
```

**Default model:** claude-opus-4-5-20251101 (Claude Opus 4.5)

### Ollama

Local LLM server:

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull a model
ollama pull llama3.2:latest

# Configure OpenTranscribe
LLM_PROVIDER=ollama
OLLAMA_API_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2:latest
```

**Default model:** llama3.2:latest

## No LLM Mode

OpenTranscribe works without any LLM configuration. Leave `LLM_PROVIDER` empty (and configure no
provider in **Settings → AI**) and these still work in full:

- **Transcription** and **speaker diarization**
- **Cross-recording speaker matching** (voiceprints are local models, not an LLM)
- **Search — keyword *and* semantic.** The embedding model runs inside your OpenSearch container;
  an embedding model is not a language model
- **Content redaction** (the optional LLM detector is off by default)
- Tags, collections, sharing, exports, subtitles, watch sources, analytics

These need a provider:

- AI summarization
- Topic / tag / collection suggestions
- Speaker identification suggestions from conversational content
- **AI Chat** — the chat page shows a setup prompt instead of a composer

Adding a provider later is retroactive: existing recordings become summarizable and chattable
immediately, with no re-processing.

Full breakdown: [Working Without an AI Model](../user-guide/without-an-ai-model.md).

## Next Steps

- [User Guide: AI Summarization](../user-guide/ai-summarization.md)
- [Environment Variables](../configuration/environment-variables.md)
- [FAQ](../faq.md)
