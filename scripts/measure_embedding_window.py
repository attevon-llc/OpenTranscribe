#!/usr/bin/env python3
"""Measure where the DEPLOYED embedding model actually truncates.

Addendum **G8** bounds the extractive digest by the embedding window and says to verify
the deployed model's real truncation before sizing. This is that verification, kept as a
script so the number in ``app/services/ingest_artifacts/sizing.py`` can be re-derived
rather than trusted.

**Why it cannot be read from configuration.** OpenSearch ML Commons' model record for
``huggingface/sentence-transformers/all-MiniLM-L6-v2`` reports
``max_position_embeddings: 512`` and states no truncation length at all; the limit lives in
the TorchScript tokenizer bundled with the model artifact. The only way to know it is to
ask the model.

**Method.** Embed a prefix; embed the same prefix with four distinctive words appended;
compare. While the prefix fits the window the two vectors differ. Once it fills the window
the appended words fall off the end and the vectors become *identical* — cosine 1.0 to
eight decimal places, not merely close. Binary-search the crossover.

Run it against an ISOLATED stack, never the live one::

    python3 scripts/measure_embedding_window.py --opensearch http://localhost:5280

Result on ``otfresh-rag403`` (2026-08-12), OpenSearch 3.4, model ``YwaF-J8BhP4lhMeewe2T``:

===========================================  =========================
Probe text                                   Vector stops changing at
===========================================  =========================
single-wordpiece filler words                126 words
rare multi-wordpiece words (~3.6 pieces ea.)  35 words  (≈126 pieces)
real QMSum transcript text off the index      92 words  (1.37 pieces/word)
===========================================  =========================

Two word-length regimes converging on ~126 *pieces* is what proves the limit is
tokenizer-side: 126 content wordpieces plus ``[CLS]``/``[SEP]`` is a **128-token window**,
half the ~256 the addendum assumed.
"""

from __future__ import annotations

import argparse
import json
import math
import urllib.request

#: Appended probe. Rare enough that its presence must move any vector that can see it.
TAIL = "zebra kaleidoscope quantum marmalade"

#: Common single-wordpiece English words, so word count ≈ wordpiece count.
SINGLE_PIECE_FILLER = (
    "the project team met to review the budget and the schedule for the next release "
    "we agreed that the plan is fine and that the work will start on monday"
).split()

#: Deliberately rare words, ~3-4 wordpieces each.
MULTI_PIECE_FILLER = "kaleidoscopic marmalade zephyrous quixotic bougainvillea".split()

#: Cosine at or above this counts as "identical" — i.e. the tail was truncated away.
IDENTICAL = 0.999999


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - operator-supplied host
        result: dict = json.load(response)
    return result


def embed(base: str, model_id: str, texts: list[str]) -> list[list[float]]:
    """Embed *texts* through ML Commons' text_embedding predict API."""
    body = _post(
        f"{base}/_plugins/_ml/_predict/text_embedding/{model_id}",
        {"text_docs": texts, "return_number": True, "target_response": ["sentence_embedding"]},
    )
    return [d["output"][0]["data"] for d in body["inference_results"]]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def find_cutoff(base: str, model_id: str, words: list[str], high: int) -> int:
    """Lowest word count at which appending TAIL no longer changes the vector."""
    low = 1
    while low + 1 < high:
        mid = (low + high) // 2
        prefix = " ".join((words * (mid // len(words) + 1))[:mid])
        first, second = embed(base, model_id, [prefix, f"{prefix} {TAIL}"])
        if cosine(first, second) >= IDENTICAL:
            high = mid
        else:
            low = mid
    return high


def deployed_model_id(base: str) -> str:
    body = _post(
        f"{base}/_plugins/_ml/models/_search",
        {"query": {"match_all": {}}, "size": 1, "_source": ["model_id"]},
    )
    return str(body["hits"]["hits"][0]["_source"]["model_id"])


def real_corpus_words(base: str, index: str) -> list[str]:
    body = _post(
        f"{base}/{index}/_search",
        {"size": 40, "_source": ["content"], "query": {"match_all": {}}},
    )
    hits = body["hits"]["hits"]
    return " ".join(h["_source"]["content"] for h in hits).split()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--opensearch",
        default="http://localhost:5280",
        help="ISOLATED stack's OpenSearch. Never point this at the live stack (5180).",
    )
    parser.add_argument("--model-id", default=None, help="Defaults to the first deployed model.")
    parser.add_argument("--index", default="transcript_chunks")
    args = parser.parse_args()

    model_id = args.model_id or deployed_model_id(args.opensearch)
    print(f"model_id: {model_id}")

    single = find_cutoff(args.opensearch, model_id, SINGLE_PIECE_FILLER, 400)
    print(f"single-wordpiece filler : truncates at {single} words")

    multi = find_cutoff(args.opensearch, model_id, MULTI_PIECE_FILLER, single)
    print(f"multi-wordpiece filler  : truncates at {multi} words")

    try:
        corpus = real_corpus_words(args.opensearch, args.index)
    except Exception as exc:  # noqa: BLE001 - the index may be empty on a fresh stack
        print(f"real corpus probe skipped ({exc})")
        corpus = []
    if len(corpus) > single:
        real = find_cutoff(args.opensearch, model_id, corpus, min(400, len(corpus)))
        print(f"real transcript text    : truncates at {real} words")
        print(f"=> wordpieces per word on real text ≈ {single / real:.3f}")

    print(f"\n=> content window ≈ {single} wordpieces; add [CLS]/[SEP] for the model's limit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
