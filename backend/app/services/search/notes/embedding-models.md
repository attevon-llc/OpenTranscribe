# search — which embedding models are offered, and why those (#504)

Moved intact from `backend/app/services/search/CLAUDE.md`.

## Which models are offered, and why those (#504)

**Every model in `OPENSEARCH_EMBEDDING_MODELS` is VERIFIED** — it registers, deploys, and
returns its declared dimension from a *real prediction*, and the multilingual ones place a
translation nearer than an unrelated sentence. `scripts/verify-embedding-models.py` is the
check; run it against a **throwaway** cluster before adding anything.

This exists because the list previously offered `paraphrase-multilingual-mpnet-base-v2`,
which **is not an OpenSearch-provided model at all** — registration FAILS at 1.0.0, 1.0.1
and 1.0.2 with *"This model is not in the pre-trained model list"*. It had reached **nine
files**. OpenSearch's provided list has `paraphrase-multilingual-MiniLM-L12-v2`
(multilingual) and `paraphrase-mpnet-base-v2` (English); the name conflated the two.

Measured 2026-08-18, `opensearch:3.4.0`, cosine of a translation against the English
original (control = an unrelated English sentence):

| model | dim | es | zh | ar | ru | control |
|---|---|---|---|---|---|---|
| all-MiniLM-L6-v2 *(default)* | 384 | 0.10 | 0.01 | 0.07 | −0.03 | −0.04 |
| all-mpnet-base-v2 | 768 | 0.09 | 0.09 | 0.10 | 0.06 | 0.02 |
| all-distilroberta-v1 | 768 | 0.16 | −0.00 | 0.07 | −0.02 | −0.03 |
| **paraphrase-multilingual-MiniLM-L12-v2** | 384 | **0.98** | **0.95** | **0.94** | **0.94** | −0.06 |
| **distiluse-base-multilingual-cased-v1** | 512 | **0.90** | **0.87** | **0.85** | **0.93** | −0.01 |

⚠️ **A high CONTROL score means the model is not cosine-trained, and this index is.**
`index_schema.py` maps `"space_type": "cosinesimil"`. Two rejected candidates scored
**0.385** (`multi-qa-mpnet-base-dot-v1`) and **0.703** (`msmarco-distilbert-base-tas-b`) on
two *unrelated* sentences — they are dot-product models, whose magnitude carries the signal
cosine discards. Ranking them here is silent and wrong, the same family as the repo-wide
`cosinesimil` conversion trap. Supporting them means a per-model `space_type`, an index
recreation, and updating all 10 kNN score-conversion sites — not a flag.

⚠️ **"Deployed" is not "working."** `all-mpnet-base-v2` on a 1 GB heap reports
`DEPLOY: COMPLETED` and then fails to embed. Verify with a prediction, never a state field.

**Heap is sized for the model, not the data** (188 MB store, 0 MB segment memory on the
measured cluster). Verified floors: 1 GB runs the default model **and**
`paraphrase-multilingual-MiniLM-L12-v2` (measured 2026-08-19: register → deploy → real
cross-lingual prediction at 1 GB, despite the model being 5× the default's size — size
does not predict the floor; mpnet is *smaller* and fails inference at 1 GB). 2 GB runs
every English model. 4 GB is the shipped default for headroom, and it is a HARD cost —
`Xms == Xmx` plus `bootstrap.memory_lock` claims and pins it. Full table:
`docker-compose.yml`.

### MEASURED: what the multilingual model buys, end to end (#453)

1,984 real MIRACL Spanish passages injected on an isolated stack, embedded through the
real ingest pipeline, 206 human-judged Spanish queries scored per arm (committed
baselines: `tests/eval/baselines/miracl-es-{multilingual,english}/`):

| arm | nDCG@10 | R@10 | MRR |
|---|---|---|---|
| `paraphrase-multilingual-MiniLM-L12-v2` | **0.7618** | 0.9049 | 0.7772 |
| `all-MiniLM-L6-v2` (the shipped default) | 0.6570 | 0.7916 | 0.6962 |
| mismatched spaces (multilingual vectors, English queries) | 0.4386 | 0.5711 | 0.5093 |

- **The default stays English — a deliberate product decision**, not an oversight: most
  deployments are English-only, the multilingual model costs ~6.5× the ingest embedding
  throughput (measured 3.24 docs/s on the OpenSearch CPU node), and its cost *on English
  corpora* is unmeasured. A multilingual deployment enables it in the Settings UI:
  pick → **Download & deploy** → Apply.
- **The mismatched row is #437's thesis, measured**: the silent mixed state costs more
  than either clean configuration. It was produced live by the reindex lock leak (fixed
  in the same change) — a switch whose re-embed was silently skipped left every vector
  in the old model's space while the pipeline embedded queries with the new one.
