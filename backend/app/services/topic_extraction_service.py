"""
AI Suggestion Service for Tags and Collections

This service uses LLM to analyze transcripts and suggest relevant tags and
collections to help users organize their media library. It follows prompt
engineering best practices from PROMPT_ENGINEERING_GUIDE.md.

Key Features:
    - Extracts 3-10 searchable tags per transcript
    - Suggests 1-3 collections for grouping related content
    - Provides confidence scores for each suggestion
    - Stores suggestions in PostgreSQL JSONB for easy access
    - Tracks user decisions for future analytics
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from typing import Optional

from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_LLM_OUTPUT_LANGUAGE
from app.core.constants import LLM_OUTPUT_LANGUAGES
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.models.prompt import UserSetting
from app.models.topic import TopicSuggestion
from app.schemas.topic import LLMSuggestionResponse
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TopicExtractionResult:
    """Plain-data outcome of one extraction run.

    Deliberately **not** the ``TopicSuggestion`` row. ``extract_topics`` writes it in
    a short session that closes before returning, so handing the caller the ORM
    instance would hand it something that lazy-loads — reopening a transaction in
    the caller's frame, which is the exact defect the phase split removes.
    """

    status: str  # "completed" | "existing"
    suggestion_uuid: str
    tag_count: int
    collection_count: int


class TopicExtractionService:
    """
    Service for extracting tag and collection suggestions from transcripts using LLM

    **Three phases, and the split is load-bearing** (see :meth:`extract_topics`):
    a short read session, the provider round trip with **no session open**, then a
    short write session. Before the split, ``extract_topics`` ran entirely on the
    session the Celery task handed it, so Postgres sat ``idle in transaction`` for
    the whole LLM call with a full ``transcript_segment`` SELECT as its last
    statement — the same shape that held an NLP worker's transaction for 1 h 26 m
    on ``ai.generate_summary``. See ``backend/app/tasks/CLAUDE.md``.

    ``db`` is therefore **optional** and is used only by the request-scoped,
    DB-only path (:meth:`apply_suggestions`). The extraction phases open their own
    short scopes and never borrow a caller's.
    """

    # System prompt for suggestion extraction (language instruction added dynamically)
    SYSTEM_PROMPT_TEMPLATE = """You are an expert content analyst specializing in media organization and categorization.

YOUR TASK:
Analyze transcripts and suggest tags and collections to help users organize their media library.{language_instruction}

TAGS:
- Short, searchable keywords (1-3 words, lowercase)
- Focus on substantive topics discussed
- Help users find content through search

COLLECTIONS:
- User-friendly group names for related content
- Help users organize their library naturally
- Descriptive but concise

YOUR APPROACH:
- Focus on content that matters (ignore small talk, logistics)
- Be specific enough to be useful, broad enough to group similar content
- Provide clear confidence scores (0.0-1.0)
- Consider what users would search for

OUTPUT STANDARD:
- Always return valid JSON matching the specified schema
- Be conservative with confidence scores (only 0.8+ for very clear suggestions)
- Suggest 3-10 tags and 1-3 collections per transcript"""

    # Main extraction prompt template (XML-structured)
    EXTRACTION_PROMPT_TEMPLATE = """<documents>
<document index="1">
  <source>transcript</source>
  <metadata>
    <file_id>{file_id}</file_id>
    <duration_seconds>{duration}</duration_seconds>
  </metadata>
  <document_content>
{transcript}
  </document_content>
</document>
</documents>

<task_instructions>
Analyze this transcript and suggest tags and collections for organizing this media file.

ANALYSIS PROCESS (use <thinking> tags to show your reasoning):
1. Read through the transcript and identify main topics
2. Ignore logistics, small talk, and formalities
3. Extract searchable tags (specific topics, keywords)
4. Suggest 1-3 collections that would group similar content
5. Provide confidence scores based on clarity

<thinking>
[Your step-by-step analysis here:
- What are the main subjects discussed?
- What tags would help someone find this content?
- What collections would naturally group this with related content?
- How confident am I in each suggestion?]
</thinking>

<answer>
Provide your response as valid JSON matching this exact schema:

{{
  "suggested_collections": [
    {{
      "name": "Collection Name",
      "confidence": 0.85,
      "rationale": "Brief explanation why this groups related content"
    }}
  ],
  "suggested_tags": [
    {{
      "name": "tag-name",
      "confidence": 0.90,
      "rationale": "Brief explanation why this tag fits"
    }}
  ]
}}
</answer>

IMPORTANT GUIDELINES:
- Extract 3-10 tags (specific topics, not generic)
- Suggest 1-3 collections maximum
- Tags should be lowercase, short (1-3 words), searchable
- Collections should be user-friendly and descriptive
- Focus on substantive content, not meeting logistics
- Confidence scores: 0.8+ for clear, 0.5-0.8 for moderate, <0.5 for uncertain
- Do NOT suggest generic tags like "discussion", "meeting", "conversation"
</task_instructions>
"""

    def __init__(self, db: Session | None = None):
        self.db = db

    def _session(self, db: Session | None) -> Session:
        """The session a DB-only helper should use: the phase's, else the caller's."""
        resolved = db if db is not None else self.db
        if resolved is None:
            raise ValueError(
                "TopicExtractionService needs a Session for this operation; "
                "construct it with one or pass db="
            )
        return resolved

    def _get_user_llm_output_language(self, user_id: int, *, db: Session | None = None) -> str:
        """
        Retrieve user's LLM output language setting from the database.

        Args:
            user_id: ID of the user
            db: Session to read through. Defaults to the one this service was
                constructed with; the extraction phases pass their own short one.

        Returns:
            LLM output language code (default: "en")
        """
        setting = (
            self._session(db)
            .query(UserSetting)
            .filter(
                UserSetting.user_id == user_id,
                UserSetting.setting_key == "transcription_llm_output_language",
            )
            .first()
        )

        if setting:
            return str(setting.setting_value)
        return DEFAULT_LLM_OUTPUT_LANGUAGE

    def _get_language_name(self, language_code: str) -> str:
        """Convert language code to full language name."""
        return LLM_OUTPUT_LANGUAGES.get(language_code, "English")

    @staticmethod
    def create_from_settings(
        user_id: int, db: Session | None = None
    ) -> Optional["TopicExtractionService"]:
        """
        Create AI suggestion service if LLM is configured for the user.

        Args:
            user_id: User ID for LLM configuration
            db: Optional caller-owned session, used only by
                :meth:`apply_suggestions`. The extraction phases open their own,
                so a Celery caller should pass nothing and hold no transaction
                across this probe.

        Returns:
            TopicExtractionService instance if LLM configured, None otherwise
        """
        try:
            # Try to create LLM service to check if configured
            llm_service = LLMService.create_from_settings(user_id=user_id)
            if llm_service:
                return TopicExtractionService(db)
            else:
                logger.info(f"LLM not configured for user {user_id}, skipping topic extraction")
                return None
        except Exception as e:
            logger.warning(f"Could not create topic extraction service: {e}")
            return None

    @staticmethod
    def _as_result(suggestion: TopicSuggestion, status: str) -> TopicExtractionResult:
        """Snapshot a ``TopicSuggestion`` as plain data, inside the session that loaded it."""
        return TopicExtractionResult(
            status=status,
            suggestion_uuid=str(suggestion.uuid),
            tag_count=len(suggestion.suggested_tags or []),
            collection_count=len(suggestion.suggested_collections or []),
        )

    def _load_extraction_inputs(
        self,
        media_file_id: int,
        force_regenerate: bool,
        redaction_cfg,
    ) -> dict[str, Any] | None:
        """Phase 1 — read (short session, Postgres only).

        Returns **plain data only**; no ORM instance escapes. An escaping instance
        would lazy-load during the provider call and silently reopen a transaction,
        reintroducing the very leak this split exists to remove.

        ``{"existing": TopicExtractionResult}`` means a suggestion is already stored
        and the caller should return it verbatim. ``None`` means there is nothing to
        extract (missing file or empty transcript).
        """
        with session_scope() as db:
            media_file = db.query(MediaFile).filter(MediaFile.id == media_file_id).first()
            if not media_file:
                logger.error(f"Media file {media_file_id} not found")
                return None

            existing = (
                db.query(TopicSuggestion)
                .filter(TopicSuggestion.media_file_id == media_file_id)
                .first()
            )
            if existing and not force_regenerate:
                logger.info(
                    f"Topic suggestion already exists for file {media_file_id}, "
                    "use force_regenerate to re-extract"
                )
                return {"existing": self._as_result(existing, "existing")}

            transcript = self._get_transcript_text(media_file, redaction_cfg, db=db)
            if not transcript:
                logger.error(f"No transcript available for file {media_file_id}")
                return None

            user_id = int(media_file.user_id)
            output_language = self._get_user_llm_output_language(user_id, db=db)
            logger.info(
                f"Topic extraction output language: {output_language} "
                f"({self._get_language_name(output_language)})"
            )
            return {
                "user_id": user_id,
                "duration": float(media_file.duration or 0),
                "transcript": transcript,
                "output_language": output_language,
            }

    def _persist_extraction(
        self, media_file_id: int, llm_response: LLMSuggestionResponse
    ) -> TopicExtractionResult | None:
        """Phase 3 — write (short session, Postgres only)."""
        with session_scope() as db:
            media_file = db.query(MediaFile).filter(MediaFile.id == media_file_id).first()
            if media_file is None:
                logger.error(f"Media file {media_file_id} disappeared during topic extraction")
                return None

            suggestion = self._store_suggestion(
                media_file=media_file,
                llm_response=llm_response,
                db=db,
            )
            if suggestion is None:
                return None
            return self._as_result(suggestion, "completed")

    def extract_topics(
        self,
        media_file_id: int,
        force_regenerate: bool = False,
        progress_callback: Callable[[str], None] | None = None,
        redaction_cfg=None,
    ) -> TopicExtractionResult | None:
        """
        Extract tag and collection suggestions from a transcript using LLM

        **Three phases, and the split is load-bearing.** A short read session
        (transcript + settings), then the provider round trip with **no session
        open**, then a short write session. This method used to run entirely on the
        session its Celery caller handed it, so Postgres sat ``idle in transaction``
        for the whole LLM call with the ``transcript_segment`` SELECT below as its
        last statement — a plain SELECT holds ACCESS SHARE for the life of its
        transaction, which queues every ``ALTER TABLE`` (i.e. any Alembic upgrade,
        which dev runs on backend startup), pins the vacuum horizon on the largest
        table in the product, and burns a pool connection.

        Args:
            media_file_id: Media file ID
            force_regenerate: Force re-extraction even if exists
            progress_callback: Optional callback function for progress updates.
                Invoked **between** phases, never inside a session scope — it
                publishes a WebSocket notification over Redis.
            redaction_cfg: Masking config from ``resolve_llm_masking``, or None when
                the owner's policy does not require pre-LLM masking. Resolved by the
                caller, not here: deciding what to do when spans are missing means
                deferring a Celery task, which is not a service's concern.

        Returns:
            A plain :class:`TopicExtractionResult`, or None when there was nothing
            to extract. **Not** the ``TopicSuggestion`` row — see that class.
        """
        # Notify: Reading transcript
        if progress_callback:
            progress_callback("Reading transcript from database...")

        # Phase 1 — read (DB session open, Postgres only).
        inputs = self._load_extraction_inputs(media_file_id, force_regenerate, redaction_cfg)
        if inputs is None:
            return None
        if "existing" in inputs:
            return inputs["existing"]  # type: ignore[no-any-return]

        # Phase 2 — the slow phase. NO DB session is held from here until the write
        # below: an LLM completion over the whole transcript.
        # ``LLMService.create_from_settings`` opens (and closes) its own short
        # session internally, which is exactly the shape the rule asks for.
        llm_service = LLMService.create_from_settings(user_id=inputs["user_id"])
        if not llm_service:
            logger.warning(f"LLM not configured for user {inputs['user_id']}")
            return None

        # Notify: Building AI prompt
        if progress_callback:
            progress_callback("Building AI prompt...")

        logger.info(
            f"Extracting suggestions for file {media_file_id} using {llm_service.config.provider}"
        )

        # Notify: Calling LLM
        if progress_callback:
            progress_callback("Calling AI model (this may take a moment)...")

        llm_response = self._call_llm_for_extraction(
            llm_service=llm_service,
            transcript=inputs["transcript"],
            file_id=media_file_id,
            duration=inputs["duration"],
            output_language=inputs["output_language"],
        )

        # Notify: Processing response
        if progress_callback:
            progress_callback("Processing AI response...")

        if not llm_response:
            logger.error(f"Failed to extract suggestions for file {media_file_id}")
            return None

        if progress_callback:
            progress_callback("Saving AI suggestions...")

        # Phase 3 — write (DB session reopened, Postgres only).
        return self._persist_extraction(media_file_id, llm_response)

    def apply_suggestions(
        self,
        suggestion_id: int,
        accepted_collections: list[str],
        accepted_tags: list[str],
    ) -> bool:
        """
        Apply user-approved tag and collection suggestions

        Args:
            suggestion_id: TopicSuggestion ID
            accepted_collections: Collection names to create/add to
            accepted_tags: Tag names to apply

        Returns:
            True if successful
        """
        # Request-scoped and DB-only: this one legitimately runs on the caller's session.
        db = self._session(None)

        # Get suggestion
        suggestion = db.query(TopicSuggestion).filter(TopicSuggestion.id == suggestion_id).first()
        if not suggestion:
            logger.error(f"Topic suggestion {suggestion_id} not found")
            return False

        try:
            # Keep status as "pending" so suggestions remain available
            # Track what user has accepted in user_decisions for analytics
            existing_decisions: dict[str, list[str]] = suggestion.user_decisions or {}  # type: ignore[assignment]
            existing_decisions.setdefault("accepted_collections", []).extend(accepted_collections)
            existing_decisions.setdefault("accepted_tags", []).extend(accepted_tags)

            # Remove duplicates
            existing_decisions["accepted_collections"] = list(
                set(existing_decisions["accepted_collections"])
            )
            existing_decisions["accepted_tags"] = list(set(existing_decisions["accepted_tags"]))

            suggestion.user_decisions = existing_decisions  # type: ignore[assignment]

            db.commit()

            logger.info(f"Applied suggestions for file {suggestion.media_file_id}")
            return True

        except Exception as e:
            logger.error(f"Error applying suggestions: {e}")
            db.rollback()
            return False

    def _get_transcript_text(
        self, media_file: MediaFile, redaction_cfg=None, *, db: Session | None = None
    ) -> str | None:
        """Extract transcript text from media file, masked per the owner's LLM policy.

        Args:
            media_file: File whose transcript to render.
            redaction_cfg: Config from ``resolve_llm_masking``, or None when the
                owner's policy does not require pre-LLM masking.
            db: Session to read through. Defaults to the one this service was
                constructed with; the read phase passes its own short one.
        """
        from app.models.media import TranscriptSegment
        from app.utils.transcript_builders import mask_segment_text

        segments = (
            self._session(db)
            .query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id == media_file.id)
            .order_by(
                TranscriptSegment.start_time,
                TranscriptSegment.end_time,
                TranscriptSegment.id,
            )
            .all()
        )

        if not segments:
            return None

        # Combine segments into full transcript
        transcript_parts = []
        for segment in segments:
            speaker_name = segment.speaker.display_name if segment.speaker else "Unknown"
            transcript_parts.append(f"{speaker_name}: {mask_segment_text(segment, redaction_cfg)}")

        return "\n".join(transcript_parts)

    def _call_llm_for_extraction(
        self,
        llm_service: LLMService,
        transcript: str,
        file_id: int,
        duration: float,
        output_language: str = "en",
    ) -> LLMSuggestionResponse | None:
        """
        Call LLM to extract suggestions from transcript with provider-specific optimizations

        Args:
            llm_service: LLM service instance
            transcript: Full transcript text
            file_id: Media file ID
            duration: Duration in seconds
            output_language: Language code for output (default: "en")

        Returns:
            Parsed LLM response or None
        """
        from app.services.llm_service import LLMProvider

        # Build language instruction for non-English output
        output_language_name = self._get_language_name(output_language)
        if output_language_name != "English":
            language_instruction = (
                f"\n\nIMPORTANT: Generate ALL tag names, collection names, and rationales "
                f"in {output_language_name}. The JSON structure should remain the same, "
                f"but all text values must be in {output_language_name}."
            )
        else:
            language_instruction = ""

        # Build system prompt with language instruction
        system_prompt = self.SYSTEM_PROMPT_TEMPLATE.format(
            language_instruction=language_instruction
        )

        # Preprocess transcript for topic extraction: remove stopwords, speaker
        # labels, timestamps to reduce token count and improve signal-to-noise.
        # NOTE: Only used for topics — summaries and speaker ID use raw text.
        from app.utils.text_preprocessing import preprocess_for_topics

        raw_len = len(transcript)
        transcript_text = preprocess_for_topics(transcript)
        logger.info(
            f"Preprocessed transcript for topics: {raw_len} chars -> {len(transcript_text)} chars "
            f"({100 - len(transcript_text) * 100 // max(raw_len, 1)}% reduction)"
        )

        # Use full context window intelligently instead of hard-coded truncation
        # Reserve tokens for system prompt, user prompt structure, and response
        # Rough estimate: 4 characters per token
        available_chars = (llm_service.user_context_window - 2000) * 4

        if len(transcript_text) > available_chars:
            logger.warning(
                f"Preprocessed transcript ({len(transcript_text)} chars) still exceeds context window, "
                f"truncating to {available_chars} chars"
            )
            transcript_text = transcript_text[:available_chars]

        # Build prompt
        prompt = self.EXTRACTION_PROMPT_TEMPLATE.format(
            file_id=file_id,
            duration=duration,
            transcript=transcript_text,
        )

        # Prepare messages with provider-specific optimizations
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        # Provider-specific optimizations
        kwargs = {"temperature": 0.1}

        if llm_service.config.provider in [LLMProvider.CLAUDE, LLMProvider.ANTHROPIC]:
            # Claude: Use response prefilling to force structured output
            messages.append({"role": "assistant", "content": "<thinking>\n"})
        elif llm_service.config.provider == LLMProvider.OLLAMA:
            # Ollama: Don't use format parameter - some models (like gpt-oss) don't support it well
            # Instead rely on prompt engineering and normal JSON extraction
            # The prompt already instructs the model to return JSON in <answer> tags
            pass

        try:
            # Call LLM with provider-specific parameters
            response = llm_service.chat_completion(messages, **kwargs)

            # Parse response
            return self._parse_llm_response(response.content, llm_service.config.provider)

        except Exception as e:
            logger.error(f"Error calling LLM for suggestion extraction: {e}")
            return None

    def _parse_llm_response(
        self, response_text: str, provider: LLMProvider
    ) -> LLMSuggestionResponse | None:
        """
        Parse LLM response and extract JSON with provider-specific handling

        Args:
            response_text: Raw LLM response
            provider: LLM provider type

        Returns:
            Parsed response or None
        """

        try:
            json_str = None

            # For all providers, try to extract JSON from <answer> tags first
            json_match = re.search(r"<answer>\s*(\{.*?\})\s*</answer>", response_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                # Try to find JSON object directly (greedy match to get full object)
                json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
                if json_match:
                    json_str = json_match.group(0)

            if not json_str:
                logger.error("Could not find JSON in LLM response")
                logger.error(f"Response text (first 1000 chars): {response_text[:1000]}")
                return None

            # Parse JSON
            data = json.loads(json_str)

            # Validate and convert to Pydantic model
            return LLMSuggestionResponse(**data)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from LLM response: {e}")
            logger.error(f"Attempted to parse: {json_str[:500] if json_str else 'None'}")
            logger.error(f"Full response text (first 1000 chars): {response_text[:1000]}")
            return None
        except Exception as e:
            logger.error(f"Error parsing LLM response: {e}")
            logger.error(f"Response text (first 1000 chars): {response_text[:1000]}")
            return None

    def _store_suggestion(
        self,
        media_file: MediaFile,
        llm_response: LLMSuggestionResponse,
        *,
        db: Session | None = None,
    ) -> TopicSuggestion | None:
        """
        Store suggestion in PostgreSQL

        Args:
            media_file: Media file instance
            llm_response: Parsed LLM response
            db: Session to write through. Defaults to the one this service was
                constructed with; the write phase passes its own short one.

        Returns:
            TopicSuggestion instance or None
        """
        session = self._session(db)
        try:
            # Convert Pydantic models to dicts for JSONB storage
            suggested_tags = [tag.dict() for tag in llm_response.suggested_tags]
            suggested_collections = [coll.dict() for coll in llm_response.suggested_collections]

            # Check if suggestion already exists
            existing = (
                session.query(TopicSuggestion)
                .filter(TopicSuggestion.media_file_id == media_file.id)
                .first()
            )

            if existing:
                # Update existing
                existing.suggested_tags = suggested_tags  # type: ignore[assignment]
                existing.suggested_collections = suggested_collections  # type: ignore[assignment]
                existing.status = "pending"  # type: ignore[assignment]
                suggestion = existing
            else:
                # Create new suggestion
                suggestion = TopicSuggestion(
                    media_file_id=media_file.id,
                    user_id=media_file.user_id,
                    suggested_tags=suggested_tags,
                    suggested_collections=suggested_collections,
                    status="pending",
                )
                session.add(suggestion)

            session.commit()
            session.refresh(suggestion)

            logger.info(
                f"Stored {len(suggested_tags)} tags and {len(suggested_collections)} collections for file {media_file.id}"
            )

            # Auto-apply high-confidence suggestions if enabled
            try:
                from app.services.auto_label_service import AutoLabelService

                auto_label_service = AutoLabelService(session)
                user_settings = auto_label_service.get_user_auto_label_settings(
                    int(media_file.user_id)
                )

                if user_settings.get("enabled", True):
                    threshold = user_settings.get("confidence_threshold", 0.75)
                    auto_label_service.auto_apply_suggestions(
                        media_file=media_file,
                        suggestion=suggestion,
                        user_id=int(media_file.user_id),
                        confidence_threshold=threshold,
                        apply_tags=user_settings.get("tags_enabled", True),
                        apply_collections=user_settings.get("collections_enabled", True),
                    )
                    logger.info(f"Auto-applied suggestions for file {media_file.id}")
            except Exception as e:
                logger.warning(f"Auto-apply failed for file {media_file.id}: {e}")

            return suggestion  # type: ignore[no-any-return]

        except Exception as e:
            logger.error(f"Error storing suggestion: {e}")
            session.rollback()
            return None
