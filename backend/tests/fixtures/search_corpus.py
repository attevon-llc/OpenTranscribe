"""Self-seeding search-quality corpus (issue: test_search_quality self-seeding).

Pure data module — no DB, no network, no OpenSearch. Six synthetic meetings authored
as :class:`app.scripts.corpus_injection.model.MeetingDoc`/``Turn`` data, injected by
``search_corpus_stack.py`` via the real corpus-injection tool so chunking, embedding
and indexing are all exercised for real (only ASR is skipped).

# ground truth:
#   query "espionage"              -> semantic gold: {sq-espionage}
#   query "surveillance"           -> keyword gold:  {sq-espionage}
#   query "space exploration"      -> semantic gold: {sq-space}
#   query "china"                  -> keyword gold:  {sq-ai-policy}
#   query "artificial intelligence"-> semantic gold: {sq-ai-policy}
#   query "fraud"                  -> keyword gold:  {sq-fraud}
#   query "scam"                   -> keyword gold:  {sq-fraud}
#   query "ancient archaeology"    -> semantic gold: {sq-archaeology}
#                                      anti-gold: contains neither "china" nor "fraud"
#   sq-airships is a pure distractor: anti-gold for every query above.
#
# GLOBAL_WORD "schedule" appears in all 6 files.
"""

from __future__ import annotations

from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import Turn

GLOBAL_WORD = "schedule"

# ── sq-espionage ─────────────────────────────────────────────────────────
_ESPIONAGE_TURNS = [
    "Ada Vance|Let's schedule this review of the signals intercept program before the quarter closes.",
    "Bo Ruiz|The covert monitoring team flagged an unusual pattern in the overseas relay traffic.",
    "Ada Vance|Right, and we might need eight more analysts if the fight over budget keeps escalating like this.",
    "Bo Ruiz|Agreed. The surveillance package for the coastal listening post is finally operational.",
    "Ada Vance|Good. Make sure the covert monitoring logs are rotated before the auditors arrive next week.",
    "Bo Ruiz|We're fighting an uphill battle getting clearance for the new intercept hardware though.",
    "Ada Vance|Understood, might as well escalate that fight to the director directly this time.",
    "Bo Ruiz|The signals intercept desk wants a joint schedule with the analysis team for cross-referencing sources.",
    "Ada Vance|One more thing: the covert monitoring budget line needs revision before the next quarterly report.",
    "Bo Ruiz|Noted. I'll draft the surveillance summary and circulate it once the numbers are confirmed.",
    "Ada Vance|Perfect, that closes out the signals intercept portion of today's covert monitoring briefing agenda.",
]

# ── sq-space ─────────────────────────────────────────────────────────────
_SPACE_TURNS = [
    "Ada Vance|We need to schedule the orbital launch window review before the fueling window closes.",
    "Cy Nkemi|The lunar landing simulation completed successfully, well within the propellant margin we budgeted.",
    "Ada Vance|Excellent. Mission control wants a full readiness briefing before the crew boards the capsule.",
    "Cy Nkemi|The orbital launch trajectory has been recalculated to avoid the debris field near the station.",
    "Ada Vance|Good call. How is the lunar landing guidance software holding up under the stress tests?",
    "Cy Nkemi|Solid so far, though we found a rare edge case in the descent throttle controller logic.",
    "Ada Vance|Let's schedule an extra simulation run focused specifically on that descent throttle edge case.",
    "Cy Nkemi|Will do. The orbital launch team also wants sign-off on the revised abort sequence procedures.",
    "Ada Vance|Approved, assuming the lunar landing rehearsal this weekend goes as smoothly as the last one.",
    "Cy Nkemi|It should. Everyone on the orbital launch crew has completed the updated emergency drills already.",
    "Ada Vance|Great, then we're on schedule for the orbital launch and the lunar landing attempt next month.",
]

# ── sq-ai-policy ─────────────────────────────────────────────────────────
_AI_POLICY_TURNS = [
    "Ada Vance|Let's schedule today's review of the export controls affecting shipments toward china this quarter.",
    "Bo Ruiz|The machine learning capability assessment flagged several models nearing the restricted performance threshold.",
    "Ada Vance|Right, and the model weights for those systems can't leave the country without a license now.",
    "Bo Ruiz|Understood. The compliance office in china has already asked for clarification on the new thresholds.",
    "Ada Vance|We'll schedule a call with their trade delegation once the china briefing document is finalized.",
    "Bo Ruiz|The machine learning training cluster export rules also need updating before the next license cycle.",
    "Ada Vance|Agreed, model weights transfers to any partner in china now require a case by case review.",
    "Bo Ruiz|The capability assessment team wants a joint schedule with legal to sort out edge cases quickly.",
    "Ada Vance|Make it happen. The china export control briefing goes to the full committee next Thursday morning.",
    "Bo Ruiz|Noted. I'll also flag the machine learning model weights question for the interagency working group.",
    "Ada Vance|Perfect, that wraps the china portion of today's export controls and model weights review agenda.",
]

# ── sq-fraud ──────────────────────────────────────────────────────────────
_FRAUD_TURNS = [
    "Ada Vance|We need to schedule the quarterly review of the payment fraud detection pipeline this week.",
    "Dee Okafor|The scam call centre network we've been tracking moved its operations to a new region entirely.",
    "Ada Vance|Concerning. How many fraud reports came in from victims of that particular scam this month?",
    "Dee Okafor|Over three hundred, and the payment fraud rings seem to be sharing infrastructure across borders now.",
    "Ada Vance|Let's schedule a briefing with the banks so they can flag the scam call centre numbers faster.",
    "Dee Okafor|Good idea. The fraud analytics team already built a model to catch these payment fraud patterns early.",
    "Ada Vance|Excellent, and does the scam detection model account for the new call centre spoofing techniques?",
    "Dee Okafor|It does now. We patched it after the last wave of payment fraud complaints came flooding in.",
    "Ada Vance|Great. Let's schedule a follow up once the updated fraud model has processed a full week of data.",
    "Dee Okafor|Will do. The scam call centre takedown is also moving forward with international law enforcement partners.",
    "Ada Vance|Perfect, that closes out the payment fraud and scam call centre portion of today's briefing agenda.",
]

# ── sq-archaeology ──────────────────────────────────────────────────────
_ARCHAEOLOGY_TURNS = [
    "Cy Nkemi|Let's schedule the next phase of the desert excavation before the seasonal winds pick up.",
    "Dee Okafor|The ancient stonework we uncovered near the ridge appears older than the site's earlier estimates.",
    "Cy Nkemi|Fascinating. Has the dating lab confirmed the age of the ancient stonework fragments we sent over?",
    "Dee Okafor|Preliminary results came back this morning, and the desert excavation site may predate the region's known settlements.",
    "Cy Nkemi|Let's schedule additional ground-penetrating radar surveys around the ancient stonework before we dig further.",
    "Dee Okafor|Agreed. The desert excavation crew also found pottery shards consistent with early trade route activity.",
    "Cy Nkemi|Wonderful. Document everything from the ancient stonework chamber before the next storm rolls through.",
    "Dee Okafor|Will do. The desert excavation team wants a joint schedule with the conservation lab for the artifacts.",
    "Cy Nkemi|Make it happen. This ancient stonework discovery could reshape our understanding of the region's history.",
    "Dee Okafor|Noted. I'll also flag the desert excavation budget for review before the dry season starts again.",
    "Cy Nkemi|Perfect, that wraps the ancient stonework portion of today's desert excavation planning session agenda.",
]

# ── sq-airships (pure distractor) ────────────────────────────────────────
_AIRSHIPS_TURNS = [
    "Bo Ruiz|Let's schedule the structural inspection of the rigid airship frame before the test flight window.",
    "Dee Okafor|The helium cell integrity checks on the rigid airship all came back within normal tolerances.",
    "Bo Ruiz|Good. How is the rigid airship engine mount redesign progressing against the weight budget target?",
    "Dee Okafor|On track, though the rigid airship team wants another vibration test before final sign-off happens.",
    "Bo Ruiz|Let's schedule that vibration test for the rigid airship engine mount early next week if possible.",
    "Dee Okafor|Will do. The rigid airship control surface actuators also need a firmware update before the flight.",
    "Bo Ruiz|Understood. Make sure the rigid airship ground crew reviews the updated mooring procedures beforehand.",
    "Dee Okafor|They already have. The rigid airship hangar schedule is clear for the whole test window now.",
    "Bo Ruiz|Excellent. Let's schedule a full readiness review once the rigid airship firmware update is verified.",
    "Dee Okafor|Noted. The rigid airship engineering team is confident the test flight will proceed without delay.",
    "Bo Ruiz|Perfect, that wraps the rigid airship portion of today's engineering readiness planning session agenda.",
]

_MEETINGS: dict[str, tuple[str, list[str]]] = {
    "sq-espionage": ("Signals Review", _ESPIONAGE_TURNS),
    "sq-space": ("Launch Readiness Sync", _SPACE_TURNS),
    "sq-ai-policy": ("Export Controls Sync", _AI_POLICY_TURNS),
    "sq-fraud": ("Fraud Ops Review", _FRAUD_TURNS),
    "sq-archaeology": ("Excavation Planning", _ARCHAEOLOGY_TURNS),
    "sq-airships": ("Airship Engineering Sync", _AIRSHIPS_TURNS),
}


def _turns(raw: list[str]) -> list[Turn]:
    result: list[Turn] = []
    clock = 0.0
    for index, line in enumerate(raw):
        speaker, text = line.split("|", 1)
        duration = max(2.0, 0.4 * len(text.split()))
        result.append(
            Turn(turn_index=index, speaker=speaker, text=text, start=clock, end=clock + duration)
        )
        clock += duration + 0.5
    return result


def build_meeting_docs() -> list[MeetingDoc]:
    """Build the six synthetic ``MeetingDoc``s for injection."""
    docs = []
    for meeting_id, (title, raw_turns) in _MEETINGS.items():
        docs.append(
            MeetingDoc(
                corpus="search_quality_fixture",
                meeting_id=meeting_id,
                title=title,
                turns=_turns(raw_turns),
                language="en",
            )
        )
    return docs


GOLD: dict[str, dict[str, set[str]]] = {
    "espionage": {
        "gold": {"sq-espionage"},
        "anti_gold": {"sq-airships", "sq-fraud", "sq-archaeology"},
    },
    "surveillance": {"gold": {"sq-espionage"}, "anti_gold": {"sq-airships"}},
    "space exploration": {"gold": {"sq-space"}, "anti_gold": {"sq-airships", "sq-fraud"}},
    "china": {"gold": {"sq-ai-policy"}, "anti_gold": {"sq-airships", "sq-archaeology"}},
    "artificial intelligence": {"gold": {"sq-ai-policy"}, "anti_gold": {"sq-airships"}},
    "fraud": {"gold": {"sq-fraud"}, "anti_gold": {"sq-airships", "sq-archaeology"}},
    "scam": {"gold": {"sq-fraud"}, "anti_gold": {"sq-airships"}},
    "ancient archaeology": {
        "gold": {"sq-archaeology"},
        "anti_gold": {"sq-airships", "sq-fraud", "sq-ai-policy"},
    },
}

ANCHOR_PHRASES: dict[str, str] = {
    "sq-espionage": "the covert monitoring logs are rotated before the auditors arrive",
    "sq-space": "we found a rare edge case in the descent throttle controller logic",
    "sq-ai-policy": "the compliance office in china has already asked for clarification",
    "sq-fraud": "the payment fraud rings seem to be sharing infrastructure across borders",
    "sq-archaeology": "the desert excavation site may predate the region's known settlements",
    "sq-airships": "the rigid airship control surface actuators also need a firmware update",
}

SPEAKER_FILE_COUNTS: dict[str, int] = {
    "Ada Vance": 4,  # sq-espionage, sq-space, sq-ai-policy, sq-fraud
    "Bo Ruiz": 3,  # sq-espionage, sq-ai-policy, sq-airships
    "Cy Nkemi": 2,  # sq-space, sq-archaeology
    "Dee Okafor": 3,  # sq-fraud, sq-archaeology, sq-airships
}

KEYWORD_QUERIES: list[str] = ["china", "fraud", "scam", "surveillance"]
SEMANTIC_QUERIES: list[str] = [
    "espionage",
    "space exploration",
    "artificial intelligence",
    "ancient archaeology",
]
