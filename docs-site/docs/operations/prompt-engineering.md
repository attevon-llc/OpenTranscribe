---
sidebar_position: 6
title: Prompt Engineering
description: Writing effective AI prompts for summarization, speaker identification, and topic extraction
---

# Prompt Engineering

OpenTranscribe uses Large Language Models for summarization, speaker identification, topic extraction, and auto-labeling. The quality of these AI outputs depends heavily on the prompts that guide the model. This guide covers how prompts work in OpenTranscribe and how to customize them for your use case.

## How OpenTranscribe Uses Prompts

OpenTranscribe separates AI instructions into two layers:

- **System prompts** define the model's role, expertise, and output standards. These set the overall behavior and are configured once per feature.
- **User prompts** contain the actual transcript content, speaker data, and task-specific instructions. These are assembled automatically for each request.

### Where Prompts Are Used

| Feature | What the Prompt Does |
|---------|---------------------|
| **Summarization** | Generates BLUF summaries with action items, decisions, and speaker analysis |
| **Speaker Identification** | Analyzes conversation context to suggest speaker names |
| **Topic Extraction** | Identifies major topics and themes from transcript content |
| **Auto-Labeling** | Suggests tags and categories for uploaded media |

The system prompt and default summarization prompt are stored in the database and can be edited through **Settings > AI > Prompts**. Per-collection prompts override the default for files within that collection.

## BLUF Summary Format

OpenTranscribe defaults to BLUF (Bottom Line Up Front) format, a military and executive communication standard that leads with the conclusion.

### Default Output Structure

Every summary includes:

1. **BLUF** (2-3 sentences) -- the key takeaway, decision, or recommendation
2. **Brief Summary** -- supporting context and details
3. **Speaker Analysis** -- who spoke, for how long, and their key contributions
4. **Action Items** -- tasks with owners, deadlines, and priorities
5. **Key Decisions** -- what was decided and why
6. **Major Topics** -- themes discussed with key points

### What Makes a Good BLUF

A strong BLUF is specific, outcome-oriented, and self-contained:

```
GOOD: "Engineering team approved Q4 roadmap with two feature deferrals
to address $50K budget overrun. Marketing budget preserved at current
levels. Sarah Chen updating roadmap by Friday."

GOOD: "Product launch delayed 2 weeks due to critical security
vulnerability discovered in testing. Security team implementing fix
with high priority. New launch date: March 15."
```

Avoid vague or passive summaries:

```
BAD: "This meeting discussed various topics including budget..."
BAD: "The team had a productive discussion..."
BAD: "We talked about Q4 and everyone shared thoughts..."
```

### Customizing the Format

To change the default summary format, edit the summarization prompt in **Settings > AI > Prompts**. You can modify the output schema, add or remove sections, or change the tone. The system validates that the response is valid JSON matching the expected structure.

## Writing Custom Prompts

### Use XML Tags for Structure

XML tags prevent the model from mixing up prompt sections and improve accuracy. OpenTranscribe uses this pattern internally:

```xml
<transcript>
{actual_transcript_content}
</transcript>

<speaker_data>
{speaker_statistics_json}
</speaker_data>

<task_instructions>
Analyze the transcript above and generate a BLUF summary.
</task_instructions>

<output_format>
{
  "bluf": "...",
  "brief_summary": "...",
  "action_items": [...]
}
</output_format>
```

Common tags: `<instructions>`, `<context>`, `<examples>`, `<transcript>`, `<output_format>`, `<constraints>`.

### Include Few-Shot Examples

Providing 2-5 concrete examples is more effective than lengthy instructions. Wrap examples in `<examples>` tags:

```xml
<examples>
<example>
<input_transcript>
John Smith [00:00]: We need to finalize the Q4 budget.
Sarah Chen [00:15]: Engineering is over budget by $50K.
John Smith [00:30]: Can we reallocate from marketing?
Mike Johnson [00:45]: Marketing budget is tight. Defer two features instead.
Sarah Chen [01:00]: Agreed. I'll update the roadmap by Friday.
</input_transcript>

<output>
{
  "bluf": "Q4 budget requires $50K reduction; team agreed to defer
           two feature releases rather than cut marketing",
  "action_items": [
    {
      "item": "Update Q4 roadmap to reflect deferred features",
      "owner": "Sarah Chen",
      "due_date": "Friday",
      "priority": "high"
    }
  ]
}
</output>
</example>
</examples>
```

Include diverse examples that cover different meeting types, edge cases, and output structures.

### Use Chain-of-Thought for Complex Analysis

For tasks requiring deep reasoning, instruct the model to think step-by-step before answering:

```xml
<instructions>
Analyze this transcript and generate a structured summary.

ANALYSIS PROCESS:
1. First, in <thinking> tags:
   - Identify main topics and themes
   - Note speaker roles and contributions
   - Extract key decisions with context
   - Identify action items with assignees

2. Then, in <answer> tags:
   - Provide the structured JSON summary
</instructions>
```

Chain-of-thought improves accuracy significantly but increases token usage and latency. It is recommended for summarization (high-value output) but not for simple extraction tasks.

## Organization Context

Organization context injects domain-specific knowledge into all AI prompts, helping the model correctly interpret jargon, acronyms, and references specific to your organization.

Configure in **Settings > AI > Organization Context**.

### Writing Effective Context

Good organization context is concise and focused on information the model would not otherwise know:

```
GOOD:
"Acme Corp is a fintech company. 'The Board' refers to the Board of
Directors. Q3 = July-September fiscal quarter. Project Phoenix is our
mobile app rewrite. Engineering teams: Platform (backend), Frontier
(frontend), Shield (security). CTO = Jane Smith, VP Eng = Bob Lee."

BAD:
"We are a company that makes software and has meetings sometimes."
```

Focus on:
- **Team and project names** that appear in meetings
- **Acronyms and abbreviations** unique to your organization
- **Key personnel** and their roles
- **Domain terminology** that might be ambiguous

The context is injected as a preamble before the transcript in all LLM calls. It applies to both summarization and speaker identification.

## Per-Collection Prompts

Collections can have their own default summarization prompt, overriding the global default for all files within that collection. This is useful when different types of content need different analysis approaches.

### Use Cases

| Collection | Prompt Focus |
|-----------|-------------|
| **Legal Depositions** | Witness statements, objections, exhibits referenced, legal terminology |
| **Team Standups** | Blockers, progress updates, commitments, brief format |
| **Customer Interviews** | Pain points, feature requests, sentiment, competitive mentions |
| **Medical Dictation** | Clinical findings, diagnoses, treatment plans, medication changes |
| **Board Meetings** | Motions, votes, resolutions, fiduciary topics |

### Setting a Collection Prompt

1. Navigate to the collection
2. Open collection settings
3. Enter a custom summarization prompt under **AI Prompt**
4. Files added to this collection will use the collection prompt instead of the global default

If a file belongs to multiple collections, the most specific prompt takes precedence.

## Speaker Identification Prompts

Speaker identification uses multi-signal analysis to suggest names for unlabeled speakers. The model looks for:

| Signal | Confidence | Example |
|--------|-----------|---------|
| **Direct name mention** | 0.95 | "Thanks John for that insight" |
| **Role reference** | 0.85 | "As the CEO, I think..." |
| **Expertise demonstration** | 0.70 | Deep knowledge in a specific domain |
| **Speech patterns** | 0.70 | Technical jargon, vocabulary complexity |
| **Conversational dynamics** | 0.60 | Who asks vs. answers questions, deference patterns |

### Improving Speaker ID Accuracy

Organization context significantly improves speaker identification. If the model knows the meeting participants, their roles, and their areas of responsibility, it can match speakers with much higher confidence.

Example context for speaker ID:

```
Meeting participants typically include:
- Dr. Sarah Chen (Chief Medical Officer) - discusses clinical trials
- James Park (Head of Regulatory) - references FDA submissions
- Maria Lopez (VP of Marketing) - discusses launch timelines
```

Speaker identification suggestions are never auto-applied. They appear as recommendations in the speaker panel for manual verification.

## Temperature and Model Settings

### Temperature

Temperature controls randomness in outputs. Lower values produce more deterministic, consistent results.

| Task | Recommended Temperature | Rationale |
|------|------------------------|-----------|
| Summarization | 0.1 | Maximum consistency and factual accuracy |
| Speaker Identification | 0.2 | Consistent analytical reasoning |
| Data Extraction | 0.1 | Deterministic, structured outputs |
| Creative Writing | 0.7-1.0 | Allow variety and expressiveness |

OpenTranscribe defaults to 0.1 for summarization and 0.2 for speaker identification. Adjust only if you need more varied output (and accept reduced consistency).

:::caution
Alter either temperature **or** top-p, not both simultaneously. Changing both creates unpredictable behavior.
:::

### Max Tokens

Max tokens controls the maximum length of the model's response. For most transcripts, the default is sufficient. Increase it for very long transcripts (2+ hours) that may produce lengthy summaries.

## Provider-Specific Tips

### vLLM (Self-Hosted)

- Best for privacy-sensitive deployments where data cannot leave your network
- Supports response prefilling (forcing JSON output format)
- Configure the endpoint as `http://<host>:<port>/v1`
- See the [Security Hardening guide](./security-hardening.md#network-security) if containers cannot reach your vLLM server

### OpenAI

- GPT-4o recommended for best quality-to-cost ratio
- Supports function/tool calling for guaranteed JSON schema compliance
- API key required; set in **Settings > AI > LLM Provider**

### Anthropic (Claude)

- Claude 3.5 Sonnet or Claude Opus 4 recommended
- Excellent at following XML-structured prompts
- Strong performance on long transcripts (200K+ token context)
- Supports response prefilling for reliable JSON output

### Ollama (Local)

- Good for development and testing
- Model quality varies significantly -- use 7B+ parameter models for summarization
- Runs on CPU if no GPU available (slower but functional)
- Configure endpoint as `http://<host>:11434`

### OpenRouter

- Access to multiple model providers through one API
- Useful for comparing model performance
- Pay-per-token pricing across providers

## Example Prompts

### Meeting Summary (Default)

```
You are a senior executive assistant with 10+ years experience
analyzing business meetings and creating actionable summaries.

YOUR EXPERTISE:
- Identifying critical business decisions and their implications
- Extracting actionable items with clear ownership
- Recognizing speaker roles and contribution patterns
- Distilling complex discussions into executive-level BLUF format

OUTPUT STANDARD:
- Follow BLUF (Bottom Line Up Front) format
- Ensure all JSON fields are properly formatted
- Provide specific evidence for all claims
- Flag low-confidence items explicitly

BLUF FORMAT REQUIREMENTS:
- First sentence: What happened / what was decided
- Second sentence: Why it matters / what's the impact
- Optional third sentence: Next critical action
- Total length: 2-3 sentences maximum
- Must be understandable without reading rest of summary
```

### Legal Deposition

```
You are a legal analyst specializing in deposition transcript review.

Focus on:
- Witness statements of fact vs. opinion
- Objections raised and their basis
- Exhibits referenced with identification numbers
- Inconsistencies or contradictions in testimony
- Key admissions or denials

Format action items as follow-up questions or investigation tasks.
Use legal terminology appropriately. Flag any testimony that
contradicts previously established facts.
```

### Medical Dictation

```
You are a medical transcription analyst with clinical terminology
expertise.

Focus on:
- Chief complaint and history of present illness
- Physical examination findings
- Assessment and diagnosis (using standard medical terminology)
- Treatment plan including medications with dosages
- Follow-up instructions and referrals

IMPORTANT: Flag any medication interactions or contraindications
mentioned. Use ICD-10 codes where diagnosis is clearly stated.
Do not infer diagnoses not explicitly stated by the provider.
```

### Podcast Episode

```
You are a content analyst for podcast production.

Focus on:
- Key topics discussed with timestamps
- Notable quotes from guests
- Audience takeaways and actionable advice
- References to external resources (books, tools, websites)
- Potential show notes and episode description

Format the BLUF as a one-paragraph episode summary suitable for
podcast directories. Keep action items focused on content the
production team needs to follow up on (fact-checking, links, etc.).
```

### Q&A Panel Extraction (community contribution)

Contributed in [issue #136](https://github.com/attevon-llc/OpenTranscribe/issues/136). This one
targets a recurring panel that answers audience-submitted questions, and produces a clickable
index of question → answer → where in the timeline it was answered.

It is a good worked example of three techniques from earlier in this page at once: XML tags for
structure, an explicit JSON output contract, and — the part most custom prompts get wrong —
**telling the model not to redo work the pipeline already did**.

````text
You are an expert content analyst with 10+ years of experience analyzing panel discussions
based on user submitted questions. You specialize in identifying questions that have clear
answers.

<task_instructions>
Analyze the provided transcript and generate a comprehensive, structured list of questions and
a summary of their answers along with a reference as to where the answer can be found in the
timeline. Your summary will be read by users who need to quickly find answers to questions.

CRITICAL REQUIREMENTS:
1. Use clear, professional language appropriate for the answer information
2. Be gender neutral in the answer summaries
3. Your response must be valid JSON matching the exact structure specified

IMPORTANT: The transcript has already been processed with speaker embedding matching. Use the
speaker information provided in SPEAKER INFORMATION section - do NOT attempt to identify or
rename speakers. Focus on analyzing content and extracting insights.
</task_instructions>

<transcript>
{transcript}
</transcript>

<speaker_information>
{speaker_data}
</speaker_information>

<output_format>
Your response must be valid JSON with this exact structure:

{{
  "Speakers": "Summary of the panel members",

  "brief_summary": "1 paragraph summary of any special topic or presentation within the transcript",

  "Questions": [
    {{
      "Question": "Exact question repeated verbatim from the transcript",
      "Answer": "Detailed summary of the answer for this question",
      "Speakers": "List the speakers who contributed to this answer",
      "timestamp_range": "[00:00] - [05:30]"
    }}
  ]
}}
</output_format>

Now analyze the provided transcript and generate your structured summary in valid JSON format.
````

Three things worth copying from it:

- **It forbids speaker re-identification explicitly.** Diarization and voiceprint matching have
  already run by the time summarization sees the transcript, and a model asked to "identify the
  speakers" will happily invent names that contradict what the pipeline resolved. Saying *do NOT
  attempt to identify or rename speakers* is what keeps the two from fighting.
- **The output contract is shown, not described.** A literal JSON skeleton is far more reliable
  than prose describing the fields.
- **"Exact question repeated verbatim"** anchors each entry to real transcript text, which is
  what makes the `timestamp_range` checkable rather than plausible.

⚠️ Note the doubled braces (`{{` / `}}`) in the JSON skeleton. Prompts are formatted with
Python's `str.format`, so a single `{` starts a placeholder — `{transcript}` and
`{speaker_data}` are real substitutions, and every *literal* brace must be doubled or
summarization fails with a `KeyError` on whatever word follows it.

## Troubleshooting

### Summaries Are Too Verbose

- Lower the temperature to 0.1
- Add explicit length constraints: "BLUF must be 2-3 sentences maximum"
- Add a negative instruction: "Do NOT include preamble, introduction, or meta-commentary"
- Provide a concrete example showing the desired length

### Summaries Miss Important Details

- Check if the transcript is being chunked (long transcripts are processed in sections)
- Add "Quote relevant evidence before summarizing" to the prompt
- Increase max tokens if the response is being truncated
- Use chain-of-thought to force the model to extract key information first

### JSON Parsing Errors

OpenTranscribe uses response prefilling to force JSON output. If you still see parsing errors:

- Check backend logs: `docker logs opentranscribe-backend | grep "Failed to parse"`
- Verify the prompt does not contain instructions that conflict with JSON output
- Ensure few-shot examples use valid JSON

### Speaker Identification Confidence Is Low

- Add organization context with participant names and roles
- Ensure the transcript has enough content per speaker (very short utterances provide little signal)
- Check that the transcript includes natural name mentions or role references
- Lower confidence thresholds are expected for speakers who are only briefly mentioned

### Hallucinated Information in Summaries

- Lower the temperature to 0.1 (minimum randomness)
- Add explicit constraints: "Only include information explicitly stated in the transcript. Do not infer or assume."
- Add edge case handling: "If information is unclear, say 'unclear from transcript' rather than guessing"
- Use chain-of-thought to force evidence extraction before conclusions

### LLM Not Generating Summaries

If summaries never appear after triggering them:

1. Verify LLM provider is configured in **Settings > AI > LLM Provider**
2. Test the connection using the **Test Connection** button
3. Check celery worker logs: `./opentr.sh logs celery-nlp-worker`
4. If using a self-hosted model (vLLM/Ollama), verify network connectivity from Docker containers -- see the [LLM Integration](../features/llm-integration.md) docs for firewall configuration
