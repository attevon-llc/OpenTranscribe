"""
Metadata-based speaker name extraction service.

Parses media metadata (title, description, channel name, tags) to extract
speaker name hints with roles (host/guest) and confidence scores. These
hints are fed as structured context to the LLM speaker identification prompt.
"""

import logging
import re
from dataclasses import dataclass
from dataclasses import field

logger = logging.getLogger(__name__)


@dataclass
class MetadataSpeakerHint:
    """A speaker name hint extracted from media metadata."""

    name: str
    role: str = "unknown"  # "host", "guest", "speaker", "unknown"
    source: str = "title"  # "title", "description", "channel"
    confidence: float = 0.5

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass
class MetadataExtractionResult:
    """Result of metadata speaker extraction."""

    hints: list[MetadataSpeakerHint] = field(default_factory=list)
    content_format: str = (
        "unknown"  # "podcast", "interview", "lecture", "panel", "meeting", "unknown"
    )

    def to_structured_context(self) -> str:
        """Format hints as structured context for LLM prompt."""
        if not self.hints:
            return ""

        lines = ["SPEAKER HINTS FROM METADATA:"]
        for hint in self.hints:
            confidence_pct = int(hint.confidence * 100)
            lines.append(
                f"  - {hint.name} (role: {hint.role}, source: {hint.source}, "
                f"confidence: {confidence_pct}%)"
            )

        if self.content_format != "unknown":
            lines.append(f"  Content format: {self.content_format}")

        return "\n".join(lines)


# Name validation pattern: must contain at least one letter and be 2-80 chars
_VALID_NAME_RE = re.compile(r"^[A-Za-z][\w\s\.\-\']{1,79}$")

# Common non-name words to filter out
_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "must",
        "can",
        "could",
        "about",
        "this",
        "that",
        "these",
        "those",
        "my",
        "your",
        "his",
        "her",
        "its",
        "our",
        "their",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "why",
        "when",
        "where",
        "episode",
        "ep",
        "part",
        "vol",
        "volume",
        "season",
        "series",
        "podcast",
        "show",
        "live",
        "stream",
        "video",
        "audio",
        "interview",
        "special",
        "bonus",
        "preview",
        "trailer",
        "intro",
        "outro",
        "recap",
        "update",
        "news",
        "daily",
        "weekly",
        "monthly",
    }
)


def _clean_name(raw: str) -> str:
    """Clean and normalize an extracted name."""
    name = raw.strip()
    # Remove surrounding quotes
    name = name.strip("\"'\u201c\u201d\u2018\u2019")
    # Remove trailing punctuation
    name = name.rstrip(".,;:!?-")
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _is_valid_name(name: str) -> bool:
    """Check if a string looks like a valid person name."""
    if not name or len(name) < 2 or len(name) > 80:
        return False

    # Must contain at least one letter
    if not re.search(r"[A-Za-z]", name):
        return False

    # Should have at least 2 words (first + last name) for higher confidence
    # Single words are allowed but will get lower confidence
    words = name.split()
    if len(words) > 6:
        return False

    # Filter out common non-name words (if the entire name is stop words)
    meaningful_words = [w for w in words if w.lower() not in _STOP_WORDS]
    return bool(meaningful_words)


def _name_confidence(name: str, source: str) -> float:
    """Calculate confidence score for an extracted name."""
    words = name.split()
    base = 0.5

    # Multi-word names are more likely real names
    if len(words) >= 2:
        base += 0.15
    if len(words) >= 3:
        base += 0.05

    # Title case suggests a proper name
    if all(w[0].isupper() for w in words if w):
        base += 0.1

    # Source-based adjustments
    if source == "title":
        base += 0.1  # Title is prominent
    elif source == "description":
        base += 0.05

    return min(base, 0.95)


class MetadataSpeakerExtractor:
    """Extracts speaker name hints from media metadata using regex patterns."""

    # Title patterns for extracting speaker names
    _TITLE_PATTERNS = [
        # "Interview with {Name}"
        re.compile(
            r"(?:interview|conversation|chat|talk)\s+with\s+(.+?)(?:\s*[-\u2013\u2014|:]|\s+on\s+|\s+about\s+|$)",
            re.IGNORECASE,
        ),
        # "Guest: {Name}" or "Host: {Name}"
        re.compile(
            r"(?:guest|host|speaker|featuring|feat\.?|ft\.?)\s*[:]\s*(.+?)(?:\s*[-\u2013\u2014|,]|$)",
            re.IGNORECASE,
        ),
        # "Featuring {Name}"
        re.compile(
            r"(?:featuring|feat\.?|ft\.?)\s+(.+?)(?:\s*[-\u2013\u2014|,]|\s+on\s+|\s+and\s+|$)",
            re.IGNORECASE,
        ),
        # "{Name} on {Topic}" (only if Name looks like a proper name)
        re.compile(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s+on\s+", re.MULTILINE),
        # "Ep. 123: {Guest} - {Topic}" or "Episode 123: {Guest} |"
        re.compile(
            r"(?:ep\.?|episode)\s*\d+\s*[:]\s*(.+?)(?:\s*[-\u2013\u2014|]|$)", re.IGNORECASE
        ),
        # "{Host} & {Guest} talk about" or "{Host} and {Guest}"
        re.compile(
            r"^(.+?)\s+(?:&|and)\s+(.+?)\s+(?:talk|discuss|chat|debate|explore)", re.IGNORECASE
        ),
        # "{Name} | {Show}" at beginning
        re.compile(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*\|", re.MULTILINE),
    ]

    # Description patterns
    _DESC_PATTERNS = [
        # "Guest: {Name}" or "Host: {Name}" on its own line
        re.compile(
            r"^(?:guest|host|speaker|panelist|moderator|presenter)\s*[:]\s*(.+?)$",
            re.IGNORECASE | re.MULTILINE,
        ),
        # "Today's guest is {Name}"
        re.compile(
            r"(?:today'?s?\s+guest|our\s+guest|special\s+guest|joined\s+by)\s+(?:is|was)?\s*(.+?)(?:[,.]|\s+(?:who|from|a\s+))",
            re.IGNORECASE,
        ),
        # "Guests: {Name1}, {Name2}, and {Name3}"
        re.compile(r"guests?\s*[:]\s*(.+?)(?:\.|$)", re.IGNORECASE | re.MULTILINE),
        # Timestamp-name: "00:00 - {Name} introduces..."
        re.compile(
            r"\d{1,2}:\d{2}(?::\d{2})?\s*[-\u2013\u2014]\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:introduces|discusses|talks|explains|shares|presents|asks|answers|responds)",
            re.MULTILINE,
        ),
        # "Hosted by {Name}" or "Presented by {Name}"
        re.compile(
            r"(?:hosted|presented|moderated|anchored)\s+by\s+(.+?)(?:[,.\n]|$)", re.IGNORECASE
        ),
        # "{Name} is a {role/title}" (bio block pattern)
        re.compile(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s+is\s+(?:a|an|the)\s+", re.MULTILINE),
    ]

    # Content format detection patterns
    _FORMAT_PATTERNS = {
        "podcast": re.compile(r"\b(?:podcast|pod|episode|ep\.\s*\d|series)\b", re.IGNORECASE),
        "interview": re.compile(r"\b(?:interview|q\s*&\s*a|conversation\s+with)\b", re.IGNORECASE),
        "lecture": re.compile(
            r"\b(?:lecture|seminar|class|course|lesson|tutorial)\b", re.IGNORECASE
        ),
        "panel": re.compile(r"\b(?:panel|roundtable|discussion|debate|forum)\b", re.IGNORECASE),
        "meeting": re.compile(
            r"\b(?:meeting|standup|stand-up|sync|retro|sprint|huddle)\b", re.IGNORECASE
        ),
    }

    def extract(self, metadata: dict) -> MetadataExtractionResult:
        """Extract speaker hints from media metadata.

        Args:
            metadata: Dictionary with keys like title, author, description,
                     source_url, metadata_raw (containing tags, categories, uploader).

        Returns:
            MetadataExtractionResult with extracted hints and detected content format.
        """
        hints: list[MetadataSpeakerHint] = []
        seen_names: set[str] = set()

        title = metadata.get("title", "") or ""
        author = metadata.get("author", "") or ""
        description = metadata.get("description", "") or ""
        metadata_raw = metadata.get("metadata_raw") or {}

        uploader = ""
        if isinstance(metadata_raw, dict):
            uploader = str(metadata_raw.get("uploader", "") or "")

        # Combine all text for format detection
        all_text = f"{title} {description}"
        content_format = self._detect_content_format(all_text)

        # Extract from title
        title_hints = self._extract_from_title(title)
        for hint in title_hints:
            name_lower = hint.name.lower()
            if name_lower not in seen_names:
                seen_names.add(name_lower)
                hints.append(hint)

        # Extract from description
        desc_hints = self._extract_from_description(description)
        for hint in desc_hints:
            name_lower = hint.name.lower()
            if name_lower not in seen_names:
                seen_names.add(name_lower)
                hints.append(hint)

        # Map channel/uploader to host hint for podcast/interview content
        if content_format in ("podcast", "interview") and (author or uploader):
            channel_name = uploader or author
            channel_name = _clean_name(channel_name)
            if _is_valid_name(channel_name) and channel_name.lower() not in seen_names:
                seen_names.add(channel_name.lower())
                hints.append(
                    MetadataSpeakerHint(
                        name=channel_name,
                        role="host",
                        source="channel",
                        confidence=0.7,
                    )
                )

        return MetadataExtractionResult(hints=hints, content_format=content_format)

    def _detect_content_format(self, text: str) -> str:
        """Detect content format from combined metadata text."""
        for fmt, pattern in self._FORMAT_PATTERNS.items():
            if pattern.search(text):
                return fmt
        return "unknown"

    def _extract_from_title(self, title: str) -> list[MetadataSpeakerHint]:
        """Extract speaker hints from the title."""
        hints: list[MetadataSpeakerHint] = []
        if not title:
            return hints

        for pattern in self._TITLE_PATTERNS:
            matches = pattern.finditer(title)
            for match in matches:
                for group_idx in range(1, len(match.groups()) + 1):
                    raw_name = match.group(group_idx)
                    if not raw_name:
                        continue

                    # Handle comma-separated names
                    names = self._split_names(raw_name)
                    for name in names:
                        name = _clean_name(name)
                        if _is_valid_name(name):
                            # Determine role from pattern context
                            role = self._infer_role_from_context(match.group(0), name)
                            hints.append(
                                MetadataSpeakerHint(
                                    name=name,
                                    role=role,
                                    source="title",
                                    confidence=_name_confidence(name, "title"),
                                )
                            )

        return hints

    def _extract_from_description(self, description: str) -> list[MetadataSpeakerHint]:
        """Extract speaker hints from the description."""
        hints: list[MetadataSpeakerHint] = []
        if not description:
            return hints

        # Limit description length for regex performance
        desc = description[:3000]

        for pattern in self._DESC_PATTERNS:
            matches = pattern.finditer(desc)
            for match in matches:
                raw_name = match.group(1)
                if not raw_name:
                    continue

                names = self._split_names(raw_name)
                for name in names:
                    name = _clean_name(name)
                    if _is_valid_name(name):
                        role = self._infer_role_from_context(match.group(0), name)
                        hints.append(
                            MetadataSpeakerHint(
                                name=name,
                                role=role,
                                source="description",
                                confidence=_name_confidence(name, "description"),
                            )
                        )

        return hints

    def _split_names(self, raw: str) -> list[str]:
        """Split a string that might contain multiple names."""
        # Handle "Name1, Name2, and Name3"
        parts = re.split(r",\s*(?:and\s+)?|\s+and\s+|\s*&\s*", raw)
        return [p.strip() for p in parts if p.strip()]

    def _infer_role_from_context(self, context: str, name: str) -> str:
        """Infer speaker role from the surrounding context."""
        ctx_lower = context.lower()
        if re.search(r"\b(?:host|hosted|anchor|present)", ctx_lower):
            return "host"
        if re.search(r"\b(?:guest|featuring|feat|ft)", ctx_lower):
            return "guest"
        if re.search(r"\b(?:speaker|panelist|moderator|presenter)", ctx_lower):
            return "speaker"
        return "unknown"


def cross_reference_attributes(
    hints: list[MetadataSpeakerHint],
    speaker_attributes: dict[str, dict],
    speaker_segments: list[dict],
) -> list[dict]:
    """Cross-reference metadata hints with voice-predicted attributes.

    Compares name-inferred gender (via the global-gender-predictor library,
    built on the WGND 2.0 dataset) with audio-predicted gender to produce
    alignment/conflict indicators.

    Args:
        hints: Speaker hints extracted from metadata.
        speaker_attributes: Dict mapping speaker_label to predicted attributes
            (from SpeakerAttributeService).
        speaker_segments: Transcript segments with speaker info for matching.

    Returns:
        List of cross-reference results:
        [
            {
                "hint_name": "Jane Doe",
                "speaker_label": "SPEAKER_01",
                "name_gender": "female",
                "voice_gender": "female",
                "alignment": "match",  # "match", "mismatch", "unknown"
                "confidence_boost": 0.1,
            },
            ...
        ]
    """
    try:
        from global_gender_predictor import GlobalGenderPredictor

        predictor = GlobalGenderPredictor()
    except ImportError:
        logger.warning("global-gender-predictor not installed, skipping cross-reference")
        return []

    results = []

    for hint in hints:
        # Extract first name for gender inference
        first_name = hint.name.split()[0] if hint.name else ""
        if not first_name:
            continue

        # Use global-gender-predictor (WGND 2.0 data) to infer gender from name
        name_gender_raw = predictor.predict_gender(first_name)

        # Map global-gender-predictor results to our categories
        # global-gender-predictor returns: "Male", "Female", "Unknown"
        # (below its 0.6 default confidence threshold collapses to "Unknown")
        name_gender = _map_gender_guess(name_gender_raw)

        if name_gender == "unknown":
            continue

        # Try to match hint to a speaker label based on predictions
        for speaker_label, attrs in speaker_attributes.items():
            voice_gender = attrs.get("predicted_gender", "unknown")

            if voice_gender == "unknown":
                alignment = "unknown"
                confidence_boost = 0.0
            elif name_gender == voice_gender:
                alignment = "match"
                confidence_boost = 0.1
            else:
                alignment = "mismatch"
                confidence_boost = -0.05

            results.append(
                {
                    "hint_name": hint.name,
                    "hint_role": hint.role,
                    "speaker_label": speaker_label,
                    "name_gender": name_gender,
                    "voice_gender": voice_gender,
                    "voice_confidence": attrs.get("gender_confidence"),
                    "alignment": alignment,
                    "confidence_boost": confidence_boost,
                }
            )

    # Filter to only meaningful alignments, sort matches first, cap at 10
    results = [r for r in results if r.get("alignment") != "unknown"]
    results.sort(key=lambda r: 0 if r.get("alignment") == "match" else 1)
    results = results[:10]
    return results


def _map_gender_guess(raw: str) -> str:
    """Map a global-gender-predictor result ("Male"/"Female"/"Unknown") to our
    simplified categories."""
    lowered = (raw or "").lower()
    if lowered == "male":
        return "male"
    elif lowered == "female":
        return "female"
    else:
        return "unknown"


def build_cross_reference_context(cross_refs: list[dict]) -> str:
    """Format cross-reference results as context for LLM prompt.

    Args:
        cross_refs: Results from cross_reference_attributes().

    Returns:
        Formatted string for inclusion in LLM context.

    Example output::

        SPEAKER ATTRIBUTE CROSS-REFERENCES:
          ✓ Joe Rogan (male name) ↔ SPEAKER_02 (voice: male 99%) — Gender match
          ✓ Sarah Anderson (female name) ↔ SPEAKER_01 (voice: female 97%) — Gender match
          ⚠ Bob Smith (male name) ↔ SPEAKER_00 (voice: female 80%) — Gender mismatch
    """
    if not cross_refs:
        return ""

    lines = ["SPEAKER ATTRIBUTE CROSS-REFERENCES:"]
    seen: set[tuple[str, str]] = set()

    for ref in cross_refs:
        alignment = ref.get("alignment")
        if alignment not in ("match", "mismatch"):
            continue

        key = (ref["hint_name"], ref["speaker_label"])
        if key in seen:
            continue
        seen.add(key)

        if alignment == "match":
            icon = "\u2713"
            note = "Gender match"
        else:
            icon = "\u26a0"
            note = "Gender mismatch"

        name_gender = ref.get("name_gender", "unknown")
        voice_gender = ref.get("voice_gender", "unknown")

        # Build voice label with optional confidence percentage
        voice_confidence = ref.get("voice_confidence")
        if voice_confidence is not None:
            confidence_pct = int(float(voice_confidence) * 100)
            voice_label = f"voice: {voice_gender} {confidence_pct}%"
        else:
            voice_label = f"voice: {voice_gender}"

        lines.append(
            f"  {icon} {ref['hint_name']} ({name_gender} name) \u2194 "
            f"{ref['speaker_label']} ({voice_label}) \u2014 {note}"
        )

    if len(lines) <= 1:
        return ""

    return "\n".join(lines)
