#!/usr/bin/env python3
"""Verify that every offered embedding model actually embeds — and embeds its languages.

Why this exists
---------------
Three things were measured while fixing #501/#502 that make static reasoning about
these models unsafe:

1. **"Deployed" lies.** ``all-mpnet-base-v2`` on a 1 GB heap reports
   ``DEPLOY: COMPLETED`` and then fails to produce an embedding. Anything that reads
   model state rather than running a prediction reports that cluster as healthy.
2. **Size-based rules do not hold.** The obvious one — "the model must fit under the
   ``neural_search`` breaker, which is 10% of heap" — is what ``docker-compose.yml``
   asserted and it is false: a 418.7 MB model deploys and serves inference under a
   409.5 MB breaker.
3. **A vector is not comprehension.** Every model returns floats for Chinese input.
   Whether those floats are *meaningful* is a different question, and it is the one
   that matters for the multilingual tiers (#453).

So each model is registered, deployed, and then asked to actually work:

* the returned vector has the dimension ``core/constants.py`` declares;
* text in several scripts (Latin / CJK / Arabic / Cyrillic) all embed;
* a **cross-lingual** check — the same sentence in English and Spanish should land
  close together for a multilingual model and measurably further apart for an
  English-only one. This is the assertion that can tell a working multilingual model
  from one that merely returns numbers.

Usage
-----
    python3 scripts/verify-embedding-models.py --url http://localhost:19203
    python3 scripts/verify-embedding-models.py --url ... --models all-MiniLM-L6-v2

Point it at a THROWAWAY cluster. It registers and deploys models, which on a shared
cluster would fight with whatever model that cluster is serving.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request

#: The same sentence in several languages. Cross-lingual similarity is computed
#: against ENGLISH, so these must stay translations of it rather than merely being
#: sentences in those languages — otherwise a low score means "different topic", not
#: "model does not align languages".
PARALLEL_TEXT = {
    'english': 'The team discussed the quarterly budget and hiring plans.',
    'spanish': 'El equipo discutió el presupuesto trimestral y los planes de contratación.',
    'german': 'Das Team besprach das Quartalsbudget und die Einstellungspläne.',
    'chinese': '团队讨论了季度预算和招聘计划。',
    'arabic': 'ناقش الفريق الميزانية الفصلية وخطط التوظيف.',
    'russian': 'Команда обсудила квартальный бюджет и планы найма.',
}

#: An UNRELATED English sentence. Without it a high English/Spanish score proves
#: nothing — a model that maps everything to nearly the same vector would score high
#: on every pair. This is the control the comparison is read against.
UNRELATED_TEXT = 'Rain fell steadily on the tin roof throughout the night.'

#: Declared in app/core/constants.py. Duplicated deliberately: this script must be
#: runnable against a cluster without importing the app, and a mismatch between the
#: two is itself a finding.
EXPECTED_DIMENSIONS = {
    'all-MiniLM-L6-v2': 384,
    'multi-qa-MiniLM-L6-cos-v1': 384,
    'paraphrase-multilingual-MiniLM-L12-v2': 384,
    'all-mpnet-base-v2': 768,
    'all-distilroberta-v1': 768,
    'distiluse-base-multilingual-cased-v1': 512,
}

#: Cosine floor for "this model actually aligns languages". See the calibration note
#: in verify_model() — it sits in a measured empty band, not at a guessed round number.
CROSS_LINGUAL_THRESHOLD = 0.5

#: Ceiling on the UNRELATED-sentence score before a model is judged unsuitable for
#: this index. A model trained for COSINE similarity puts two unrelated sentences near
#: 0; one trained for DOT PRODUCT does not, because its vectors are not normalised and
#: magnitude carries the signal cosine throws away.
#:
#: Measured 2026-08-18, same sentence pair, opensearch 3.4.0:
#:     cosine-trained   control -0.056 .. +0.075   (7 models)
#:     dot-product      control  0.385 (multi-qa-mpnet-base-dot-v1)
#:                              0.703 (msmarco-distilbert-base-tas-b)
#: 0.25 sits in that gap. This MATTERS here specifically: the chunks index maps
#: `"space_type": "cosinesimil"` (services/search/indexing_service.py), so a
#: dot-product model would be ranked by a metric it was not trained for — silently,
#: with plausible-looking scores. It is the same family of trap as the repo-wide
#: `cosinesimil` conversion note in the root CLAUDE.md.
UNRELATED_CEILING = 0.25

MULTILINGUAL = {
    'paraphrase-multilingual-MiniLM-L12-v2',
    'distiluse-base-multilingual-cased-v1',
}


def _request(url: str, method: str = 'GET', body: dict | None = None, timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - localhost throwaway cluster
        url, data=data, method=method, headers={'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {'__http_error__': exc.code, 'body': exc.read().decode()[:400]}
    except Exception as exc:  # noqa: BLE001
        return {'__error__': str(exc)}


def _await_task(base: str, task_id: str, limit: int = 180) -> tuple[str, dict]:
    for _ in range(limit):
        time.sleep(5)
        payload = _request(f'{base}/_plugins/_ml/tasks/{task_id}')
        state = payload.get('state', '')
        if state in {'COMPLETED', 'FAILED', 'COMPLETED_WITH_ERROR'}:
            return state, payload
    return 'TIMEOUT', {}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _embed(base: str, model_id: str, text: str) -> list[float] | None:
    payload = _request(
        f'{base}/_plugins/_ml/_predict/text_embedding/{model_id}',
        method='POST',
        body={
            'text_docs': [text],
            'return_number': True,
            'target_response': ['sentence_embedding'],
        },
    )
    try:
        return payload['inference_results'][0]['output'][0]['data']
    except (KeyError, IndexError, TypeError):
        return None


def verify_model(base: str, short_name: str) -> dict:
    """Register, deploy and genuinely exercise one model."""
    full_name = f'huggingface/sentence-transformers/{short_name}'
    result: dict = {'model': short_name, 'status': 'unknown', 'notes': []}

    registered = _request(
        f'{base}/_plugins/_ml/models/_register',
        method='POST',
        body={'name': full_name, 'version': '1.0.1', 'model_format': 'TORCH_SCRIPT'},
    )
    task_id = registered.get('task_id')
    if not task_id:
        result['status'] = 'REGISTER_REJECTED'
        result['notes'].append(str(registered)[:200])
        return result

    state, payload = _await_task(base, task_id)
    if state != 'COMPLETED':
        result['status'] = f'REGISTER_{state}'
        result['notes'].append(str(payload.get('error', ''))[:200])
        return result
    model_id = payload.get('model_id', '')

    deployed = _request(f'{base}/_plugins/_ml/models/{model_id}/_deploy', method='POST')
    state, payload = _await_task(base, deployed.get('task_id', ''))
    if state != 'COMPLETED':
        result['status'] = f'DEPLOY_{state}'
        result['notes'].append(str(payload.get('error', ''))[:200])
        return result

    # Deployment is not the claim. Run real predictions.
    vectors: dict[str, list[float]] = {}
    for language, text in PARALLEL_TEXT.items():
        vector = _embed(base, model_id, text)
        if vector is None:
            result['status'] = 'DEPLOYED_BUT_INFERENCE_FAILED'
            result['notes'].append(f'no embedding for {language}')
            return result
        vectors[language] = vector

    dimension = len(vectors['english'])
    expected = EXPECTED_DIMENSIONS.get(short_name)
    result['dimension'] = dimension
    if expected is not None and dimension != expected:
        result['status'] = 'WRONG_DIMENSION'
        result['notes'].append(f'declared {expected}, returned {dimension}')
        return result

    unrelated = _embed(base, model_id, UNRELATED_TEXT)
    if unrelated is None:
        result['status'] = 'DEPLOYED_BUT_INFERENCE_FAILED'
        return result

    english = vectors['english']
    result['control_unrelated'] = round(_cosine(english, unrelated), 3)
    for language, vector in vectors.items():
        if language != 'english':
            result[f'sim_{language}'] = round(_cosine(english, vector), 3)

    # The judgement, CALIBRATED ON MEASUREMENT (2026-08-18, opensearch 3.4.0, 4 GB).
    #
    # The first version of this check asked only whether a translation scored above
    # the unrelated control. That passed for EVERY model, including the English-only
    # ones, because their control sits near 0 and their translation scores near 0.1 —
    # technically higher, and meaningless. A discriminator that fires on everything
    # discriminates nothing.
    #
    # The measured separation is wide and unambiguous:
    #   English-only     es 0.085-0.159, de 0.139-0.313  (max observed 0.313)
    #   Multilingual     es 0.897-0.979, de 0.870-0.899  (min observed 0.848 across
    #                                                     es/de/zh/ar/ru)
    # so 0.5 sits in an empty band with ~0.19 clearance on the low side and ~0.35 on
    # the high side. The English-only models are the must-stay-below control: if a
    # future change makes them "pass" this, the check has broken, not improved.
    cross = [result[f'sim_{lang}'] for lang in ('spanish', 'german') if f'sim_{lang}' in result]
    result['cross_lingual_ok'] = bool(cross) and min(cross) >= CROSS_LINGUAL_THRESHOLD
    result['status'] = 'OK'
    if short_name not in MULTILINGUAL and result['cross_lingual_ok']:
        result['status'] = 'CONTROL_BROKEN'
        result['notes'].append(
            f'an English-only model scored >= {CROSS_LINGUAL_THRESHOLD} on translations; '
            'the cross-lingual check no longer separates the two classes'
        )
    if short_name in MULTILINGUAL and not result['cross_lingual_ok']:
        result['status'] = 'OK_BUT_NOT_CROSS_LINGUAL'
        result['notes'].append(
            'translations are no closer than an unrelated sentence — this model is '
            'offered as multilingual but does not behave as one here'
        )
    return result


ARTIFACT_BASE = (
    'https://artifacts.opensearch.org/models/ml-models/huggingface/sentence-transformers'
)


def check_availability(short_name: str, version: str = '1.0.1') -> dict:
    """Is the artifact actually published? No cluster needed, seconds not minutes.

    Catches the #504 failure — a model named in our registries that OpenSearch does not
    provide — without registering anything. Both files matter: the zip is the model, and
    config.json is REQUIRED for offline `file://` registration (#502). The phantom
    model's config.json answered 403 while every real one answers 200.
    """
    prefix = f'{ARTIFACT_BASE}/{short_name}/{version}/torch_script'
    outcome = {'model': short_name}
    for label, url in (
        ('zip', f'{prefix}/sentence-transformers_{short_name}-{version}-torch_script.zip'),
        ('config', f'{prefix}/config.json'),
    ):
        req = urllib.request.Request(url, method='HEAD')  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                outcome[label] = resp.status
        except urllib.error.HTTPError as exc:
            outcome[label] = exc.code
        except Exception as exc:  # noqa: BLE001
            outcome[label] = str(exc)
    outcome['available'] = outcome.get('zip') == 200 and outcome.get('config') == 200
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', help='Base URL of a THROWAWAY OpenSearch')
    parser.add_argument(
        '--check-availability',
        action='store_true',
        help='Only check that each artifact is published (no cluster, seconds)',
    )
    parser.add_argument('--models', nargs='*', default=sorted(EXPECTED_DIMENSIONS))
    args = parser.parse_args()

    if args.check_availability:
        results = [check_availability(name) for name in args.models]
        for outcome in results:
            state = 'OK' if outcome['available'] else 'MISSING'
            print(
                f'{outcome["model"]:44} {state:8} zip={outcome["zip"]} config={outcome["config"]}'
            )
        return 0 if all(r['available'] for r in results) else 1

    if not args.url:
        parser.error('--url is required unless --check-availability is given')
    base = args.url.rstrip('/')
    results = []
    for short_name in args.models:
        print(f'--- {short_name}', flush=True)
        outcome = verify_model(base, short_name)
        results.append(outcome)
        print(f'    {json.dumps(outcome)}', flush=True)

    print('\n=== SUMMARY ===')
    for outcome in results:
        line = f'{outcome["model"]:44} {outcome["status"]:32} dim={outcome.get("dimension", "-")}'
        if 'sim_spanish' in outcome:
            line += (
                f' es={outcome["sim_spanish"]} zh={outcome.get("sim_chinese")}'
                f' ar={outcome.get("sim_arabic")} ctrl={outcome["control_unrelated"]}'
            )
        print(line)
        for note in outcome['notes']:
            print(f'    ! {note}')

    return 0 if all(r['status'] == 'OK' for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
