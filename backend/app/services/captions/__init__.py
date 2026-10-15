"""Caption cue segmentation — the one implementation behind every caption surface.

The file-detail player's caption track (``GET /files/{uuid}/captions``), the subtitle
download (``GET /files/{uuid}/subtitles``), the bulk subtitle ZIP, the burned-in-subtitle
video and the SRT/WebVTT transcript export all cut cues here, so what a user watches and
what they download agree. Rules and their sources: ``rules.py``.
"""

from app.services.captions.model import CaptionSegment
from app.services.captions.model import CaptionTrack
from app.services.captions.model import Cue
from app.services.captions.model import CuePart
from app.services.captions.render import to_srt
from app.services.captions.render import to_webvtt
from app.services.captions.rules import CJK_RULES
from app.services.captions.rules import LATIN_RULES
from app.services.captions.rules import CaptionRules
from app.services.captions.segmenter import build_caption_track

__all__ = [
    "CJK_RULES",
    "LATIN_RULES",
    "CaptionRules",
    "CaptionSegment",
    "CaptionTrack",
    "Cue",
    "CuePart",
    "build_caption_track",
    "to_srt",
    "to_webvtt",
]
