# Prompt Engineering Guide for OpenTranscribe

This guide documents best practices for prompt engineering based on Anthropic's official Claude documentation, specifically tailored for OpenTranscribe's transcription summarization and speaker identification features.

## Table of Contents

1. [Core Principles](#core-principles)
2. [XML Structure](#xml-structure)
3. [Few-Shot Examples](#few-shot-examples)
4. [Chain of Thought](#chain-of-thought)
5. [System Prompts](#system-prompts)
6. [Long Context Management](#long-context-management)
7. [Structured Output](#structured-output)
8. [Temperature Settings](#temperature-settings)
9. [Application-Specific Patterns](#application-specific-patterns)

---

## Core Principles

### 1. Be Clear and Direct

**The Golden Rule:** If you show your prompt to a colleague with no context and they're confused, Claude will be too.

**Best Practices:**
- Treat Claude like a brilliant but new employee who needs explicit instructions
- Provide contextual information: purpose, audience, workflow, success criteria
- Use sequential, numbered steps for multi-step tasks
- Explicitly state constraints and requirements

**Example:**
```
❌ BAD: "Summarize this meeting."

✅ GOOD: "You are an expert meeting analyst. Analyze this business meeting transcript and create a BLUF (Bottom Line Up Front) summary. Focus on decisions made, action items with owners, and key discussion points. The summary will be read by executives who need to quickly understand outcomes."
```

### 2. Show Don't Tell

Use concrete examples rather than abstract instructions. 3-5 diverse examples dramatically improve accuracy and consistency.

---

## XML Structure

**Why:** XML tags prevent Claude from mixing up prompt sections, improve accuracy, and make parsing easier.

**Best Practices:**
- Use descriptive, meaningful tag names
- Nest tags for hierarchical content
- Separate instructions, examples, and context with tags
- Common tags: `<instructions>`, `<context>`, `<examples>`, `<transcript>`, `<output_format>`

**Example:**
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

---

## Few-Shot Examples

**Impact:** Providing 3-5 examples is more effective than lengthy instructions for improving output quality.

**Guidelines:**
- **Quantity:** Start with 3-5 examples; more is better
- **Quality:** Make examples relevant, diverse, and clear
- **Structure:** Wrap in `<examples>` and `<example>` tags
- **Coverage:** Include edge cases

**Example:**
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
  "bluf": "Q4 budget requires $50K reduction; team agreed to defer two feature releases rather than cut marketing",
  "brief_summary": "Leadership meeting addressed Q4 budget overrun...",
  "action_items": [
    {
      "item": "Update Q4 roadmap to reflect deferred features",
      "owner": "Sarah Chen",
      "due_date": "Friday",
      "priority": "high"
    }
  ],
  "key_decisions": [
    {
      "decision": "Defer two feature releases to address budget overrun",
      "context": "Engineering over budget by $50K",
      "impact": "Q4 roadmap will be updated"
    }
  ]
}
</output>
</example>

<example>
[Another diverse example showing different structure...]
</example>
</examples>
```

---

## Chain of Thought

**When to Use:** Complex analysis tasks requiring deep reasoning (summarization, speaker matching).

**How:** Ask Claude to think step-by-step before providing the answer.

**Techniques:**

1. **Basic:** "Think step-by-step"
2. **Structured:** Use `<thinking>` and `<answer>` tags

**Example:**
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

**Trade-offs:**
- ✅ Dramatically improves accuracy
- ✅ Enables debugging of reasoning
- ❌ Increases token usage and latency
- **Recommendation:** Use for summarization (high-value), not simple tasks

---

## System Prompts

**Most Powerful Tool:** The `system` parameter is the most effective way to customize Claude's behavior.

**Best Practices:**
- Put role-specific instructions in `system` parameter
- Task-specific details go in `user` message
- Be specific with role definitions

**Example:**
```python
# ❌ GENERIC
system = "You are a helpful assistant."

# ✅ SPECIFIC
system = """You are a senior executive assistant with 10+ years experience analyzing business meetings and creating actionable summaries.

YOUR EXPERTISE:
- Identifying critical business decisions and their implications
- Extracting actionable items with clear ownership
- Recognizing speaker roles and contribution patterns
- Distilling complex discussions into executive-level BLUF format

YOUR APPROACH:
- Prioritize decisions and actions over general discussion
- Maintain objectivity and precision
- Identify ambiguities and flag them clearly
- Use business-appropriate language

OUTPUT STANDARD:
- Follow BLUF (Bottom Line Up Front) format
- Ensure all JSON fields are properly formatted
- Provide specific evidence for all claims
- Flag low-confidence items explicitly"""
```

**Impact Example from Anthropic:**
- Generic role: Basic summary
- "General Counsel" role: Identifies risks and provides strategic recommendations
- "CFO" role: Provides nuanced strategic financial briefing

---

## Long Context Management

### 1. Document Placement

**Key Finding:** Placing documents (~20K+ tokens) at the **top** of prompts improves response quality by up to 30%.

**Structure:**
```xml
<documents>
<document index="1">
  <source>transcript</source>
  <metadata>
    <duration>45 minutes</duration>
    <speaker_count>4</speaker_count>
  </metadata>
  <document_content>
{full_transcript}
  </document_content>
</document>

<document index="2">
  <source>speaker_statistics</source>
  <document_content>
{speaker_data_json}
  </document_content>
</document>
</documents>

<task_instructions>
{your_instructions}
</task_instructions>

<examples>
{few_shot_examples}
</examples>
```

### 2. Quote Extraction

**Key Finding:** Asking Claude to quote relevant parts first improves recall from 27% to 98% in Anthropic's tests.

**Technique:**
```python
# Prefill Claude's response to force quote extraction first
messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": user_prompt},
    {"role": "assistant", "content": "Let me identify the most relevant evidence:\n\nRELEVANT QUOTES:\n"}
]
```

### 3. Intelligent Chunking

**Best Practices:**
- Split at natural boundaries (speaker changes, topics, sentences)
- Add overlap context between chunks
- Preserve continuity with previous chunk summaries
- Never split mid-sentence or mid-speaker turn

**Current Implementation:** OpenTranscribe already uses speaker-boundary chunking ✓

---

## Structured Output

### Reliability Techniques (in order of effectiveness):

1. **Tool/Function Calling** (Most Reliable)
   - Force Claude to use tool with defined schema
   - Claude Sonnet 3.5 returned valid JSON 1,000+ times without errors
   - Recommended for production

2. **Response Prefilling**
   - Prefill Assistant message with `{` to force JSON
   - Bypasses friendly preamble
   - Works well for consistent formatting

3. **Schema Validation**
   - Use runtime validation (Pydantic, Zod)
   - Validate and retry on malformed responses

4. **Prompt Engineering**
   - Precise JSON format specification
   - Provide examples
   - Affects ~14-20% of requests

**Example - Response Prefilling:**
```python
messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": user_prompt},
    {"role": "assistant", "content": "{"}  # Force JSON start
]
```

### Error Handling

**Defensive Prompting:**
```
EDGE CASE HANDLING:

IF speaker is mentioned by name directly:
  - Confidence = 0.95
  - Evidence type: "direct_mention"
  - Include exact quote

IF speaker's role is clearly stated:
  - Confidence = 0.85
  - Evidence type: "role_reference"

IF no clear evidence exists:
  - DO NOT provide a prediction
  - Add to "unidentified_speakers" list
  - Explain what context would help

IF multiple possible matches exist:
  - Provide top 2 candidates
  - Explain distinguishing factors
  - Recommend manual verification
```

---

## Temperature Settings

**Temperature** controls randomness in outputs. Lower = more deterministic, higher = more creative.

### Recommended Settings:

| Task Type | Temperature | Rationale |
|-----------|-------------|-----------|
| Summarization | 0.1 | Maximum consistency and factual accuracy |
| Speaker Identification | 0.2 | Consistent analytical reasoning |
| Data Extraction | 0.1 | Deterministic outputs |
| Question Answering | 0.2 | Factual, low randomness |
| Creative Writing | 0.7-1.0 | Allow variety and creativity |

**Important:** Alter either temperature OR top-p, not both.

**Current OpenTranscribe Implementation:**
- Summarization: 0.1 ✓ (updated from 0.3)
- Speaker ID: 0.2 ✓

---

## Application-Specific Patterns

### BLUF Format (Bottom Line Up Front)

**Definition:** Military/executive communication standard - start with the conclusion.

**Structure:**
1. **BLUF** (2-3 sentences): Key takeaway, decision, recommendation
2. **Background**: Supporting details
3. **Action Items**: Who, what, when
4. **Decisions**: What was decided and why

**Good BLUF Examples:**
```
✅ "Engineering team approved Q4 roadmap with two feature deferrals to address $50K budget overrun. Marketing budget preserved at current levels. Sarah Chen updating roadmap by Friday."

✅ "Product launch delayed 2 weeks due to critical security vulnerability discovered in testing. Security team implementing fix with high priority. New launch date: March 15."
```

**Bad BLUF Examples:**
```
❌ "This meeting discussed various topics including budget..." (too vague)
❌ "The team had a productive discussion..." (no concrete outcome)
❌ "We talked about Q4 and everyone shared thoughts..." (no decisions)
```

**Prompt Guidance:**
```
BLUF FORMAT REQUIREMENTS:
- First sentence: What happened / what was decided
- Second sentence: Why it matters / what's the impact
- Optional third sentence: Next critical action
- Total length: 2-3 sentences maximum
- No preamble or introduction
- Must be understandable without reading rest of summary
```

### Speaker Identification

**Multi-Signal Analysis:**

1. **Direct name mentions** (confidence: 0.95)
   - "Thanks John for that insight"
   - "As Sarah mentioned earlier..."

2. **Role references** (confidence: 0.85)
   - "As the CEO, I think..."
   - "From an engineering perspective..."

3. **Speech patterns** (confidence: 0.70)
   - Vocabulary complexity
   - Technical jargon usage
   - Communication style

4. **Expertise demonstration** (confidence: 0.70)
   - Deep knowledge in specific domains
   - Authority patterns

5. **Conversational dynamics** (confidence: 0.60)
   - Who asks vs. answers questions
   - Deference patterns

**Quote Extraction Enhancement:**
```python
# Extract specific evidence FIRST, then identify
evidence_prompt = f"""
<transcript>
{transcript}
</transcript>

<task>
Find all quotes where {speaker_label} is:
1. Directly mentioned by name
2. Role is explicitly stated
3. Self-identifies themselves

Output these specific quotes before analysis.
</task>
"""
```

### Action Item Extraction

**Well-Defined Action Items:**

Required fields:
1. **Who**: Specific person responsible
2. **What**: Clear, actionable task (verb-first)
3. **When**: Deadline or timeframe
4. **Priority**: High/Medium/Low
5. **Context**: Why this is needed (1 sentence)

**Example Schema:**
```json
{
  "item": "Update Q4 roadmap to reflect deferred features",
  "owner": "Sarah Chen",
  "due_date": "Friday, Oct 13",
  "priority": "high",
  "context": "Engineering budget overrun requires feature deferrals",
  "dependencies": ["Budget approval from finance"],
  "mentioned_timestamp": "[15:30]"
}
```

**Good Action Items:**
```
✅ "Update roadmap by Friday" (clear verb, deadline, owner)
✅ "Schedule security review with DevOps team before deployment" (specific, actionable)
```

**Bad Action Items:**
```
❌ "Roadmap needs updating" (passive voice, no owner/deadline)
❌ "Someone should look into the bug" (vague, no owner)
❌ "Follow up on that thing we discussed" (unclear what action)
```

---

## Testing and Evaluation

### Success Criteria

Good criteria should be:
- **Specific**: Clearly defined metrics
- **Measurable**: Quantifiable outcomes
- **Achievable**: Realistic given constraints
- **Relevant**: Aligned with business goals

### Evaluation Dimensions

For OpenTranscribe specifically:

1. **Task Fidelity**: Does it do what was asked?
2. **Consistency**: Same input → similar output
3. **Fact Accuracy**: No hallucinations
4. **Relevance**: Focuses on important content
5. **Coherence**: Well-structured, logical flow
6. **Tone**: Professional and appropriate
7. **Latency**: Response time acceptable
8. **Cost**: Token efficiency

### A/B Testing Framework

```python
class PromptTester:
    """Test prompt variations for quality and consistency"""

    def test_prompt_variant(
        self,
        prompt_template: str,
        test_cases: list[dict],
        metrics: list[str]
    ) -> dict:
        """
        Test prompt across multiple cases

        Returns metrics: accuracy, consistency, avg_tokens, latency
        """
        results = []
        for test_case in test_cases:
            output = llm_service.generate_summary(
                test_case["transcript"],
                test_case["speaker_data"]
            )

            score = self._evaluate(
                output,
                test_case["expected"],
                metrics
            )
            results.append(score)

        return {
            "avg_score": mean(results),
            "consistency": stdev(results),
            "details": results
        }
```

---

## Quick Reference

### Priority 1 Improvements (High Impact, Low Effort)

✅ **Add XML Structure**
- Wrap transcript, speaker data, instructions in XML tags
- Files: `backend/app/core/default_prompts.py`, `llm_service.py`

✅ **Implement Response Prefilling**
- Add `{"role": "assistant", "content": "{"}` to force JSON
- Files: `llm_service.py`

✅ **Lower Temperature**
- Change from 0.3 to 0.1 for summarization
- Files: `llm_service.py`

✅ **Add BLUF Guidelines**
- Provide concrete examples in prompts
- Files: `backend/app/core/default_prompts.py`

### Priority 2 Improvements (Medium Impact, Medium Effort)

✅ **Add Few-Shot Examples**
- Included in summarization prompt
- Files: `backend/app/services/llm_service.py`

✅ **Quote Extraction for Speaker ID**
- Prefill response requests quotes first
- Files: `backend/app/services/llm_service.py` `identify_speakers` method

✅ **Per-Collection Prompts**
- Custom system prompts scoped to collections
- See "Per-Collection Prompts" section above

✅ **Org Context Injection**
- Organization-level context block prepended to system prompts
- Configured via Settings → LLM → Organization Context

⏳ **Context Overlap in Chunking**
- Add previous summary to next chunk context
- Files: `llm_service.py` `_chunk_transcript_intelligently`

### Priority 3 Improvements (High Impact, High Effort)

🔮 **Tool-Based Structured Output**
- Use Claude's tool calling for guaranteed JSON
- Files: `llm_service.py` - new method

🔮 **Self-Correction Chain**
- Have Claude review its own summaries
- Files: `llm_service.py` - new method

---

## Resources

### Official Documentation
- [Prompt Engineering Overview](https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/overview)
- [Long Context Tips](https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/long-context-tips)
- [System Prompts](https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/system-prompts)
- [Multishot Prompting](https://docs.claude.com/en/docs/build-with-claude/prompt-engineering/multishot-prompting)

### Research Papers
- [Prompting for Long Context](https://www.anthropic.com/news/prompting-long-context)
- [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents)
- [Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)

---

## Application-Specific Features (v0.4.0)

### Per-Collection Prompts

OpenTranscribe v0.4.0 supports custom system prompts scoped to individual collections. This allows different teams or use-cases to get purpose-specific summaries without affecting other users.

**How it works:**
- Each collection can store a `default_prompt` in the database (added in migration `v190_add_collection_default_prompt.py`)
- At summarization time, the system resolves the prompt in priority order:
  1. File-level override (if set)
  2. Collection-level prompt (if the file belongs to a collection with a custom prompt)
  3. User-level default prompt
  4. System-level default prompt

**Best practices for collection prompts:**
- Keep collection prompts focused on the domain (e.g., "legal depositions", "sales calls", "medical interviews")
- Include role-specific instructions in the collection prompt rather than the user default
- Collection prompts are inherited by all files added to that collection

**Example — Legal deposition collection:**
```xml
<task_instructions>
You are a legal analyst specializing in deposition summaries. Analyze this deposition transcript and produce a structured legal brief.

Focus on:
- Witness admissions and concessions
- Contradictions with prior statements
- Key facts established under oath
- Areas where counsel objected

Output must be in formal legal language suitable for inclusion in court filings.
</task_instructions>
```

### Organizational Context Injection

OpenTranscribe can inject an organization-level context block into prompts to improve relevance for enterprise deployments. Configure via `Settings → LLM → Organization Context`.

**What to include in org context:**
- Organization name and domain
- Key personnel titles (e.g., "CEO is referred to as 'the executive'")
- Domain-specific terminology glossary
- Standard output format requirements

**Example org context:**
```
Organization: Acme Corporation (healthcare software)
Domain: Healthcare IT, EHR systems, HIPAA compliance
Key roles: CTO (technology decisions), CPO (product roadmap), CMO (clinical affairs)
Terminology: "the platform" = Acme's EHR product, "providers" = healthcare providers (not software vendors)
Output: All summaries must flag any mention of patient data or HIPAA implications.
```

When org context is configured, it is prepended to the system prompt for all summarization tasks.

### Disabling AI Summary Per File or Per User

Users can disable automatic AI summary generation:

**Per-user toggle** (Settings → AI Features → Auto-Summary):
- When disabled, the "Generate Summary" button is still available for manual triggering
- Useful for users who want to control when LLM API calls are made
- Setting stored in `user_settings` table

**Per-file disable:**
- Set `ai_summary_enabled=false` on the file record via the API or UI
- Summary button is hidden in the file detail view
- Added in migration `v351_add_ai_summary_settings.py`

**Prompt engineering implication:** When building custom integrations, check `ai_summary_settings.enabled` before constructing and sending prompts. Never call the LLM if the user or file has disabled summaries.

---

## Changelog

### v0.4.0 - 2026-03
- Added per-collection prompt documentation
- Added organizational context injection guidance
- Added AI summary disable toggle documentation
- Updated Quick Reference to reflect implemented vs. planned status

### v1.0 - 2025-01-03
- Initial guide created from Anthropic's official documentation
- Tailored for OpenTranscribe's summarization and speaker identification use cases
- Comprehensive coverage of core principles, advanced techniques, and application patterns
