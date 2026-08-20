# Modern RAG for High-Accuracy LLM Systems: Retrieval, Graphs, Databases, Validation, Scale, and the Coding Ecosystem

## Executive assessment

The first architectural decision I would make is **not to pick a single “RAG database.”** For a serious enterprise system, the strongest design is usually a **multi-retriever evidence architecture** in which relational data, document search, vectors, and optionally graphs do different jobs.

A practical target architecture is:

```text
                           ┌─────────────────────────┐
                           │     User question       │
                           └────────────┬────────────┘
                                        │
                              Intent / query router
                                        │
              ┌─────────────────────────┼──────────────────────────┐
              │                         │                          │
        Exact / structured        Document / semantic       Relationship /
             question                  question               multi-hop
              │                         │                          │
         SQL / NoSQL         BM25 + dense vectors       Graph / GraphRAG
              │                         │                          │
              └──────────────┬──────────┴─────────────┬────────────┘
                             │                        │
                      Candidate fusion        Graph/path evidence
                             │                        │
                             └───────────┬────────────┘
                                         │
                                    Reranker
                                         │
                               Diversity / dedupe
                                         │
                                  Evidence pack
                                         │
                                      LLM
                                         │
                        Claim + citation verification
                                         │
                             Answer or abstention
```

The evidence strongly favors **hybrid retrieval as the default baseline** rather than pure dense-vector search. A 2026 benchmark covering 23,088 questions over 7,318 mixed text-and-table financial documents found that hybrid retrieval followed by neural reranking achieved Recall@5 of 0.816 and MRR@3 of 0.605, outperforming the tested single-stage methods; notably, BM25 itself beat the tested state-of-the-art dense retrieval approach on this financial corpus. The authors explicitly recommend hybrid retrieval plus reranking and warn that general retrieval leaderboards did not reliably predict performance in their financial domain. citeturn12academia34turn0search3

That result captures one of the biggest lessons in modern RAG:

> **Do not optimize for embeddings. Optimize for evidence recovery.**

Exact identifiers, product numbers, legislation references, filenames, transaction IDs, error codes, dates, financial figures, and specialist terminology frequently benefit from lexical retrieval. Semantic retrieval is better at paraphrases and conceptual similarity. Systems such as Qdrant, Weaviate, Elasticsearch, OpenSearch, Pinecone, and LlamaIndex consequently expose ways to combine lexical and semantic retrieval instead of forcing developers to choose one. citeturn7search1turn7search2turn8search0turn8search1turn8search3turn5search10

**GraphRAG should be considered a specialized retrieval channel rather than a replacement for ordinary RAG.** Recent research is strikingly task-dependent. A 2026 comparison found graph retrieval only marginally better on general QA but substantially better on multi-hop QA, while other evaluations find GraphRAG can underperform vanilla RAG on straightforward or time-sensitive questions. citeturn10search25turn0search4

There is an equally important caveat: **successful retrieval does not guarantee a correct answer.** A 2026 study of graph-based RAG found the gold answer was present in retrieved context for 77–91% of tested questions, yet answer accuracy ranged from only 35–78%; the study attributed 73–84% of errors to reasoning rather than retrieval. This is one of the strongest arguments for evaluating your retriever, evidence selector, and generator separately. citeturn10academia31

For large databases, do not embed information that can be answered deterministically. **SQL should remain SQL.** A question such as “How much revenue did customer X generate during Q2?” is fundamentally different from “What do our policies say about exceptions for customer X?” The former belongs in a relational query; the latter belongs in document retrieval. Current enterprise text-to-SQL benchmarks demonstrate why this separation matters: Spider 2.0 contains databases with more than 1,000 columns and real enterprise-style workflows, and early evaluated agent systems solved only a small fraction of those tasks despite performing much better on older benchmarks. citeturn11search1

My overall recommendation is therefore:

| Requirement | Preferred first approach |
|---|---|
| Exact facts, counts, aggregates, dates | SQL / relational query |
| JSON application records | NoSQL/document query + filters |
| Natural-language document QA | BM25 + dense hybrid search |
| Highest document precision | Hybrid + cross-encoder/late-interaction reranker |
| Exact identifiers + conceptual questions | Hybrid lexical/dense |
| Multi-hop entity relationships | Graph retrieval + vector/text fallback |
| Corpus-wide “themes / relationships / communities” | GraphRAG |
| Complex ambiguous research questions | Agentic multi-stage retrieval |
| Tables + text | Structured/table-aware retrieval + lexical/vector retrieval |
| Very large corpus | ANN first stage + reranking |
| High-stakes answers | Multi-retriever + provenance + verification + abstention |

The remainder explains why.

## The RAG retrieval landscape and what actually improves accuracy

The term “RAG” now hides many substantially different retrieval architectures. Treating them as one technology leads to poor engineering decisions.

| Method | What it does well | Where it fails | My recommendation |
|---|---|---|---|
| **BM25 / lexical RAG** | Exact language, identifiers, rare terms | Paraphrases and semantic equivalence | Always benchmark |
| **Dense-vector RAG** | Semantic similarity and paraphrases | Exact tokens, numerical/entity distinctions, redundancy | Useful but rarely sufficient alone |
| **Hybrid RAG** | Combines lexical + semantic evidence | Requires fusion/ranking design | Best general baseline |
| **Hybrid + reranker** | High recall followed by high precision | Additional latency/compute | Best quality-oriented baseline |
| **Multi-vector / late interaction** | Fine-grained query-document matching | More storage/compute | Excellent reranking/high-precision option |
| **Multi-query RAG** | Ambiguous questions, vocabulary mismatch | Duplicate/noisy candidates, more LLM calls | Use conditionally |
| **HyDE/query expansion** | Some zero-shot semantic retrieval problems | Can hurt exact/numerical questions | Benchmark rather than default |
| **Diversity-aware retrieval** | Reduces near-duplicate context | May sacrifice close matches if overtuned | Valuable for broad/multi-hop queries |
| **GraphRAG** | Relations, entity neighborhoods, multi-hop reasoning | Graph extraction cost/errors; weak on some simple QA | Add only where graph structure matters |
| **Structured/SQL RAG** | Exact structured facts | Natural-language ambiguity/schema grounding | Use for structured systems of record |
| **Agentic RAG** | Iterative research and heterogeneous sources | Cost, latency, unpredictable retrieval depth | Use for complex questions, not every query |
| **Corrective/adaptive RAG** | Reacts when evidence looks inadequate | More system complexity | Valuable once basic evaluation is mature |

### Why hybrid retrieval is such a strong baseline

Dense embeddings and lexical retrieval have complementary failure modes. Qdrant's current search documentation explicitly supports dense, sparse, and multivector retrieval together with RRF or score-based fusion; Weaviate's hybrid search combines vector retrieval with BM25F; Elasticsearch supports Reciprocal Rank Fusion across standard and kNN retrieval; OpenSearch similarly provides score normalization and rank-fusion pipelines. citeturn7search1turn7search2turn8search0turn8search1

Pinecone's documentation makes the same distinction particularly clearly: semantic retrieval can miss exact domain-specific terminology while lexical retrieval can miss synonyms and paraphrases, so its hybrid functionality combines dense and sparse signals. Its current platform also distinguishes BM25 full-text fields from learned sparse-vector representations. citeturn8search3turn8search12

A good production retrieval sequence is consequently often:

```text
query
  │
  ├── BM25 / full text ─────────────── top 50-200
  │
  └── dense ANN search ─────────────── top 50-200
                   │
             RRF / fusion
                   │
             top 50-100
                   │
        neural / late-interaction
               reranker
                   │
             top 5-15
                   │
          dedupe / diversify
                   │
               LLM
```

This gives the cheap retrieval stage responsibility for **recall**, and the more expensive reranker responsibility for **precision**. Weaviate explicitly describes reranking as a second-stage operation because applying an expensive relevance model to the entire corpus would normally be prohibitively costly; Haystack likewise supports cross-encoder and late-interaction rankers after a broader retriever. citeturn7search6turn3search4turn3search10

### Why cosine similarity by itself is not enough

A vector similarity score does not tell you that a document contains the answer. It only indicates closeness according to the chosen embedding representation and metric.

Capital One's 2026 DF-RAG research focuses directly on this problem. The researchers found similarity-oriented retrieval could return redundant evidence, then developed query-adaptive diversity retrieval using maximal marginal relevance. Across five QA benchmarks, their reported system improved F1 by 4–10% over vanilla RAG, with their oracle analysis suggesting substantially larger potential gains when retrieval diversity is selected appropriately for each query. citeturn2search2

Weaviate now exposes MMR as a query-time reranking operation as well, retrieving a larger initial candidate pool before trading some redundancy for diversity. citeturn7search38

A practical evidence set should therefore optimize approximately:

```text
utility(document) =
    relevance
  + complementary_information
  + source_quality
  + recency_when_needed
  + authority
  - redundancy
  - contradiction_risk
```

rather than merely:

```text
utility(document) = cosine_similarity
```

### Query rewriting, expansion, and multi-query retrieval

Generating multiple versions of a query can recover documents that one query formulation misses, but it is not free. Every rewritten query introduces more candidates, more latency, and often more duplicates.

The 2026 text-and-table retrieval benchmark found query-expansion techniques such as HyDE and multi-query retrieval provided limited benefit on precise numerical questions and recommended avoiding HyDE for domains dominated by precise numbers or entity-specific questions. citeturn12academia34

This suggests a better architecture:

```text
Simple exact query
      ↓
single hybrid retrieval

Ambiguous semantic query
      ↓
query rewrite / expansion
      ↓
hybrid retrieval

Multi-hop question
      ↓
question decomposition
      ↓
several targeted retrieval steps

Relationship question
      ↓
entity resolution
      ↓
graph retrieval + document evidence
```

Do **not** invoke expensive query rewriting for every user request.

### Contextual, hierarchical, and long-document retrieval

Fixed-size chunking is easy but often destroys document structure. Headers, tables, definitions, footnotes, and preceding context can determine the meaning of a sentence.

Current managed RAG platforms are moving beyond simple fixed chunks. Amazon Bedrock Knowledge Bases exposes different parsing/chunking approaches and its current managed offering includes smart parsing that preserves document metadata and handles multimodal content. citeturn1search0turn1search11

The 2026 retrieval benchmark also found contextual index augmentation produced consistent moderate gains on its financial corpus, unlike query expansion, which was less effective for precise questions. citeturn12academia34

For documents such as:

```text
annual reports
contracts
regulations
technical manuals
research papers
medical literature
engineering specifications
large knowledge bases
```

you should test parent-child retrieval, document+chunk retrieval, semantic chunking, and hierarchical metadata rather than assuming one arbitrary token chunk size is optimal.

LlamaIndex, for example, exposes chunk-and-document hybrid retrieval in which both document-level and chunk-level similarity contribute to candidate selection. citeturn5search26

### Structured RAG and text-to-SQL

Embedding relational database rows is often the wrong abstraction for structured questions.

Consider:

> “Which customers purchased products in category A, had more than three support incidents, and generated at least $1 million in the last twelve months?”

The database already contains exact join and aggregation semantics. Translating every record into prose and performing vector search throws those semantics away.

The preferred architecture is:

```text
natural-language question
          │
      intent router
          │
     schema retrieval
          │
   relevant tables/columns
          │
     text-to-SQL model
          │
     SQL validation
          │
   read-only execution
          │
      result checks
          │
     LLM explanation
```

BIRD explicitly evaluates text-to-SQL on both correctness and efficiency, including questions that require external knowledge beyond the schema. citeturn11search3 Spider 2.0 goes substantially further toward enterprise complexity, with real database environments, multiple SQL dialects, metadata/documentation search, and workflows far beyond simple single-query generation. citeturn11search1

Recent systems increasingly use execution itself as a verification mechanism. ReFoRCE, for example, performs iterative column exploration and query execution before self-refining generated SQL, illustrating why database interaction is often more reliable than simply giving a model the schema once and accepting its first query. citeturn11search17

This area is progressing rapidly, but it is far from solved. The difference between results on older text-to-SQL benchmarks and Spider 2.0 is strong evidence that impressive benchmark claims on small schemas should not be interpreted as production readiness. citeturn11search1

## Graph databases versus relational, NoSQL, and vector retrieval

The useful question is not “Which database is best?” but:

> **Which information relationships need to be preserved at retrieval time?**

### Relational databases

Relational systems are excellent systems of record when correctness depends on structured schemas, joins, constraints, transactions, aggregates, dates, and exact filtering.

PostgreSQL plus pgvector is particularly interesting for RAG because it lets the same database perform normal relational operations and vector retrieval. pgvector performs exact nearest-neighbor retrieval by default; HNSW and IVFFlat indexes are optional approximate indexes that trade recall for speed. citeturn7search0

That is extremely useful for **testing retrieval correctness**:

```text
Exact vector search = oracle
Approximate HNSW result = candidate implementation

Recall@k =
    |ANN_top_k ∩ Exact_top_k|
    ─────────────────────────
              k
```

You can run exact searches on a sample of production queries and quantify exactly how much recall you lose by introducing ANN indexing.

A relational system is my preferred starting point when the organization already has PostgreSQL expertise and the RAG application needs strong relationships between application records, metadata, authorization information, and vectors.

### Search engines

For document-heavy RAG, Elasticsearch and OpenSearch are unusually attractive because full-text retrieval is their native strength rather than an afterthought.

Elasticsearch can combine kNN and standard textual search using RRF without requiring the raw relevance scores from the two methods to be comparable. citeturn8search0turn8search4

OpenSearch similarly provides hybrid search combining keyword and semantic queries, normalization/fusion, filters, and an `explain` facility that can expose how hybrid scores were calculated. citeturn8search1turn8search7

For corpora dominated by:

```text
documentation
tickets
email
legal text
support records
manuals
policies
logs
knowledge articles
```

I would benchmark an Elastic/OpenSearch-style system very seriously before purchasing an additional vector-only database.

### Vector-native databases

Qdrant, Weaviate, Milvus, and Pinecone are all credible modern retrieval platforms, but their important differences are operational rather than simply “how good are their vectors.”

Qdrant currently supports dense, sparse, and multivector searches, RRF/DBSF fusion, metadata filters, BM25/full-text retrieval, late-interaction patterns, and vector quantization. citeturn7search1turn7search13turn7search17turn7search29

Weaviate exposes BM25, vectors, hybrid retrieval, MMR, filters, and second-stage reranking. citeturn7search2turn7search6turn7search18turn7search38

Milvus provides multiple ANN index families, including in-memory and disk-oriented approaches such as HNSW and DiskANN; its DiskANN documentation specifically describes keeping the graph index on disk for datasets that cannot economically fit entirely in memory. citeturn7search3turn7search7

Pinecone's current data model supports semantic, sparse, full-text/BM25, metadata, hybrid retrieval, and hosted reranking patterns. citeturn8search8turn8search11turn8search16

The critical point is that **“vector database” no longer necessarily means “vector-only search.”** Modern products are converging toward multiple ranking signals.

### NoSQL/document databases

Document-oriented databases make sense when the original knowledge objects are JSON-like application records and the application needs flexible metadata, filters, and semantic search alongside those records.

MongoDB Atlas Vector Search is one example of this convergence; Amazon's current Bedrock integrations likewise treat MongoDB Atlas as one of several supported vector-backed knowledge-base stores. citeturn8search2turn1search7

NoSQL is generally most attractive when the application already has a large operational document model. It becomes less attractive when correctness depends predominantly on complicated multi-table joins or deep relationship traversal.

### Graph databases

Graphs model information explicitly as entities and relationships:

```text
(Customer)-[:OWNS]->(Account)
(Account)-[:MADE]->(Transaction)
(Transaction)-[:PAID]->(Merchant)
(Merchant)-[:OWNED_BY]->(Company)
(Company)-[:DIRECTOR]->(Person)
```

A graph query can follow this structure directly.

Neo4j additionally supports vector indexes, meaning graph traversal and semantic search do not necessarily require separate databases. Its current documentation explicitly supports similarity searching over vector properties and hybridizing those signals with other ranked retrieval sources. citeturn9search17

Amazon Neptune supports graph-oriented query models such as Gremlin, openCypher, and SPARQL, making it suitable for property-graph and RDF-oriented architectures. citeturn9search2

The important distinction is:

> **A vector-index graph such as HNSW is not a knowledge graph.**

HNSW's edges exist so an ANN algorithm can navigate a vector space. Knowledge-graph edges represent meaningful domain relationships. pgvector, for example, describes HNSW as one of its ANN index options, whereas GraphRAG systems construct graphs from semantic entities and relationships. citeturn7search0turn6search28

### Where GraphRAG really helps

Microsoft's GraphRAG architecture extracts entities and relationships from text, constructs a graph, organizes it into hierarchical communities, produces community summaries, and then uses those structures during retrieval. citeturn6search28

That gives graphs several potentially powerful capabilities that flat similarity search does not naturally provide:

```text
entity → neighboring entities
entity → relationships
entity → related source chunks
entity → paths
entity → communities
community → corpus summary
question → connected evidence spanning documents
```

The research evidence increasingly suggests that this is particularly useful when questions are **relational or genuinely multi-hop**. A 2026 comparison reported only marginal GraphRAG gains over dense retrieval on general QA but an average improvement of roughly 27 points across the tested multi-hop benchmarks. citeturn10search25

Other studies report GraphRAG or hybrid GraphRAG improving factual correctness or context relevance in specific technical domains, reinforcing the conclusion that graph value is highly workload-dependent. citeturn0search0

### Where GraphRAG can be worse

Graphs introduce an entirely new error pipeline:

```text
source document
    ↓
entity extraction
    ↓
entity resolution
    ↓
relationship extraction
    ↓
graph construction
    ↓
community detection
    ↓
graph retrieval
    ↓
context generation
    ↓
LLM reasoning
```

Every arrow can be wrong.

That explains why graph retrieval is not universally superior. Recent analyses report GraphRAG underperforming ordinary RAG on some conventional QA tasks, including poorer performance on simple/local and time-sensitive questions in tested settings. citeturn0search4

Another key result is the previously mentioned reasoning bottleneck: even when GraphRAG successfully retrieved text containing the answer, downstream reasoning frequently remained the dominant error. citeturn10academia31

Graphs can therefore add unnecessary expense for a corpus where most questions look like:

> “What is our PTO policy?”

> “What is product X's warranty?”

> “What was the Q4 revenue figure?”

Those questions do not inherently require graph traversal.

Graphs become much more compelling for:

> “Which suppliers are connected to components involved in these three incidents?”

> “How are these subsidiaries, executives, contracts, and transactions connected?”

> “Which concepts connect these research areas across the corpus?”

> “What dependencies would be affected if this service were removed?”

### A practical database selection matrix

| Technology | Exact data | Semantic text | Lexical text | Deep relationships | Aggregation | Typical role |
|---|---:|---:|---:|---:|---:|---|
| PostgreSQL | Excellent | Good with pgvector | Good/possible | Good for defined joins | Excellent | Canonical structured store + vectors |
| Elastic/OpenSearch | Good | Excellent | Excellent | Limited compared with graph | Good | Large document retrieval |
| MongoDB | Excellent for document records | Good | Available | Moderate | Good | Operational JSON + retrieval |
| Qdrant | Metadata-centric | Excellent | Strong current support | Limited | Limited relative to SQL | Vector/hybrid retrieval |
| Weaviate | Metadata-centric | Excellent | Strong | Some object references | Limited | Hybrid AI retrieval |
| Milvus | Metadata-centric | Excellent | Hybrid capabilities | Limited | Limited | Very large vector workloads |
| Pinecone | Metadata/document-centric | Excellent | Strong current support | Limited | Limited | Managed hybrid/vector retrieval |
| Neo4j | Excellent graph facts | Vector supported | Can combine search | **Excellent** | Graph-oriented | Knowledge graph / GraphRAG |
| Neptune | Excellent graph facts | Architecture-dependent | Architecture-dependent | **Excellent** | Graph-oriented | Managed enterprise graph |

The winning enterprise architecture can easily contain **three of these categories simultaneously**.

## Validating graph results, database queries, and the source of truth

This is arguably more important than the retrieval technology itself.

You should distinguish four different ideas:

```text
Data integrity
    Is the stored data structurally valid?

Retrieval correctness
    Did the search/query retrieve the correct evidence?

Answer groundedness
    Does the LLM's answer follow from that evidence?

Truth/provenance
    Does that evidence trace back to an authoritative source?
```

They require different tests.

### Never make an extracted graph the ultimate source of truth

For an LLM-generated knowledge graph, I recommend every entity and especially every relationship carry provenance fields conceptually like:

```json
{
  "edge_id": "e_802139",
  "subject": "Supplier-A",
  "predicate": "SUPPLIES_COMPONENT",
  "object": "Component-X",

  "source_document_id": "doc_1298",
  "source_version": "sha256:...",
  "source_page": 18,
  "source_span_start": 10942,
  "source_span_end": 11038,

  "extraction_model": "model-version",
  "extraction_pipeline": "kg-pipeline-v12",
  "extracted_at": "timestamp",

  "valid_from": "date",
  "valid_to": null,

  "human_verified": false
}
```

That allows the application to transform:

```text
Graph claim
Supplier A → SUPPLIES → Component X
```

back into:

```text
Original PDF
→ page 18
→ exact paragraph
→ immutable document version
```

The graph should therefore be treated as an **index over evidence**, not the evidence itself. This recommendation follows directly from GraphRAG's use of automatically extracted entities/relationships and from the documented variability of GraphRAG correctness across tasks. citeturn6search28turn0search4

### Enforce graph schemas mechanically

For RDF graphs, W3C SHACL exists specifically to validate RDF graphs against formally defined shapes and constraints; the latest SHACL work also defines structured validation reports. citeturn9search0turn9search4

For example:

```text
Employee:
    must have employee_id exactly once
    employee_id must be unique
    must belong to >= 1 business unit

Contract:
    must have effective_date
    must have counterparty
    expiration_date >= effective_date

OWNS:
    Person|Company -> Company
    ownership_pct between 0 and 100
```

Property-graph databases provide analogous protections. Neo4j supports uniqueness, property-existence, key, and other graph constraints that can prevent structurally invalid nodes or relationships from being introduced. citeturn9search1turn9search37

### Validate graph extraction separately from graph retrieval

Do not measure only final QA accuracy.

Create a manually verified sample and calculate:

```text
Entity precision
Entity recall

Relationship precision
Relationship recall

Entity-resolution precision
Entity-resolution recall

Source-span accuracy

Path retrieval Recall@K

Graph answer accuracy
```

A graph can have excellent QA on one benchmark while containing a large number of incorrect edges that happened not to affect those questions. Conversely, a high-quality graph can retrieve the right subgraph and still fail because the language model reasons incorrectly, exactly the failure pattern observed in recent GraphRAG research. citeturn10academia31

GraphRAG-Bench was designed in part to address this problem by evaluating multiple stages, including graph construction, retrieval, and answer generation rather than only judging the final response. citeturn10search1turn10search7

### Differentially test graph and relational representations

Suppose a relational source of truth contains:

```text
customers
accounts
account_owners
transactions
companies
directors
```

and you project it into a graph.

Build a suite where the same logical questions are expressed both ways.

Example:

```sql
SELECT ...
FROM company c
JOIN directors d
  ON ...
JOIN person p
  ON ...
WHERE ...
```

versus:

```cypher
MATCH (p:Person)-[:DIRECTOR_OF]->(c:Company)
WHERE ...
RETURN ...
```

For every test, compare:

```text
entity IDs
row counts
aggregations
NULL behavior
duplicates
date ranges
relationship cardinalities
```

This is vastly more powerful than asking an LLM whether a result “looks correct.”

The same philosophy underlies text-to-SQL execution benchmarks such as BIRD and Spider 2.0: ultimately, generated database queries must execute and return the intended results rather than merely look syntactically plausible. citeturn11search3turn11search1

### Test relational databases continuously

Tools such as dbt provide reusable tests for `unique`, `not_null`, `accepted_values`, and referential `relationships`, and allow custom SQL tests for domain-specific invariants. citeturn9search3turn9search7turn9search15

A high-accuracy RAG ingestion pipeline should fail before indexing if tests such as these fail:

```text
document_id must be unique
source_uri must not be null
document_version must not be null

chunk_id must be unique
chunk → document relationship must exist

entity_id must be unique
edge → source_chunk must exist

ACL principal must exist
effective_date <= expiration_date

embedding_model_version must be known
chunk_hash must correspond to source text
```

The retriever cannot produce trustworthy results from a broken ingestion pipeline.

### Test ANN retrieval against exact retrieval

Approximate-nearest-neighbor search deliberately trades some recall for performance. pgvector's documentation makes this explicit: exact search provides perfect nearest-neighbor recall, while HNSW and IVFFlat trade some recall for speed. citeturn7search0

Therefore periodically run:

```text
same query
   │
   ├── exact vector scan → gold nearest neighbors
   │
   └── ANN search        → production result
                    │
              compare top-K
```

Measure:

```text
ANN Recall@1
ANN Recall@5
ANN Recall@10
p50 latency
p95 latency
p99 latency
```

Then tune HNSW/IVF parameters until your recall/latency curve matches your business requirement.

This is one of the easiest RAG accuracy checks to automate, yet it is frequently overlooked.

### Validate hybrid retrieval explanations

Search engines can expose why documents were selected. OpenSearch's hybrid-search explanation facilities can show normalization, combination, and subquery scoring information, although the documentation correctly warns that explanation is relatively expensive and therefore better suited to debugging than every production request. citeturn8search7

Keep retrieval traces containing:

```text
query
rewritten queries
retriever type
filters
retrieved IDs
raw scores
fusion ranks
reranker scores
final context
source IDs
LLM answer
citations
latency per stage
```

Without these traces, a bad RAG response is nearly impossible to diagnose.

## Benchmarks, datasets, and an evaluation system you can actually trust

Public benchmarks are useful, but they should be used to **bootstrap your evaluation program, not replace it**.

### What to measure

A serious evaluation dashboard should separate the pipeline.

| Layer | Metrics |
|---|---|
| Ingestion | parse success, OCR/table accuracy, source coverage, duplicate rate |
| Graph construction | entity/edge precision and recall, provenance accuracy |
| Retrieval | Recall@K, Precision@K, Hit@K, MRR, nDCG |
| ANN | recall relative to exact nearest neighbors |
| Reranking | nDCG/MRR change before vs. after rerank |
| Evidence | source coverage, diversity, duplicate rate |
| Generation | exact match/F1 where applicable, factual correctness |
| Grounding | supported-claim rate, citation precision/recall |
| SQL | execution accuracy, result equivalence |
| Operations | p50/p95/p99 latency, QPS, memory, storage, index time |
| Freshness | source-to-index lag, stale-result rate |
| Security | unauthorized-document retrieval rate |
| Business | successful task rate, escalation rate, user corrections |

RAGPerf is a useful emerging benchmark framework because it decomposes RAG into embedding, indexing, retrieval, reranking, and generation while recording end-to-end throughput, CPU/GPU/memory behavior and quality measurements rather than collapsing the system into one answer score. citeturn12search2turn12search5

### Useful public datasets and benchmark classes

**GraphRAG-Bench** is one of the most directly relevant options for your graph question. It was built specifically to test GraphRAG, including graph construction, retrieval and answer generation, with challenging domain-oriented multi-hop questions. citeturn10search1

**HotpotQA, 2WikiMultiHopQA, and MuSiQue** remain commonly used research workloads for multi-hop QA and are repeatedly used in current graph/multi-hop retrieval studies. They are useful for testing whether a system can assemble evidence across documents rather than find one matching paragraph. citeturn10search13turn10search4

**BIRD** is useful when you are evaluating natural-language querying of relational data. It emphasizes correct and efficient SQL and includes problems where external business knowledge is needed in addition to the schema. citeturn11search3

**Spider 2.0** is particularly valuable for enterprise database work because its 632 problems were built around much more realistic database workflows, including complex schemas, different SQL systems, project-level context, and extremely long queries. The gap between performance on Spider 1.0 and Spider 2.0 is a warning against assuming older text-to-SQL results translate directly to enterprise databases. citeturn11search1

**ANN-Benchmarks** provides a reproducible environment for evaluating approximate-nearest-neighbor algorithms across speed/recall tradeoffs. citeturn12search1turn12search4

**VectorDBBench/VDBBench** provides a framework for testing vector database performance under controlled workloads, including performance/cost dimensions. Because it originated from a vector-database vendor ecosystem, I would use the tooling but rerun it on your own hardware and corpus rather than accepting a published leaderboard as procurement evidence. citeturn12search0

**BigVectorBench** targets shortcomings in simple vector benchmarks by including heterogeneous embeddings and more complex query patterns. citeturn12search20

### Ragas and application-level evaluation

Ragas is now maintained as a toolkit for evaluating and optimizing LLM/RAG applications and can also generate evaluation test sets when an organization does not yet have one. citeturn10search3turn10search12

The important warning is that automated LLM judges should never be your only ground truth.

A robust evaluation set should combine:

```text
human-authored questions
+
real anonymized production questions
+
known-answer deterministic database questions
+
hard negatives
+
ambiguous questions
+
unanswerable questions
+
multi-hop questions
+
freshness questions
+
authorization/security questions
```

For each production incident, add a regression case. Over time this becomes more valuable than a generic leaderboard.

### Build your own “golden corpus”

For an enterprise deployment I would construct approximately these classes of tests:

| Test class | What it exposes |
|---|---|
| Obvious single-document questions | Basic indexing failures |
| Exact-name questions | Dense-search weaknesses |
| Paraphrase questions | Lexical-search weaknesses |
| Numerical questions | Hallucination and precision problems |
| Multiple similar documents | Reranker quality |
| Contradictory documents | Authority/version handling |
| Stale versus new document | Freshness |
| Multi-hop questions | Graph/decomposition value |
| No-answer questions | Abstention behavior |
| Unauthorized document questions | ACL leakage |
| Table questions | Parsing/structured retrieval |
| Rare entity questions | Entity-resolution problems |
| Relationship questions | Graph retrieval quality |
| Large result-set SQL | Aggregation/query validation |

Then compare every architecture using **the exact same corpus, queries, model, and answer grader**.

That is how you determine whether graph retrieval, relational retrieval, or a new vector database genuinely improves your application.

## Modern RAG coding packages and infrastructure to investigate

There is no reason to limit the investigation to two packages. The modern RAG software landscape spans orchestration, ingestion, retrieval, graph construction, evaluation, and storage.

### Coding frameworks

| Package | Strongest use | What I would use it for | Important caveat |
|---|---|---|---|
| **LangChain** | Large integration ecosystem | General RAG and tools | Abstraction can become complex |
| **LangGraph** | Stateful workflows | Agentic/corrective RAG | More engineering than simple 2-step RAG |
| **LlamaIndex** | Data ingestion/index/retrieval | Advanced document and graph RAG | Many options require careful benchmarking |
| **Haystack** | Explicit pipelines | Production retrieval/reranking | More pipeline-oriented than agent-first |
| **DSPy** | Program optimization | Optimizing RAG/agent behavior against metrics | Needs a good evaluation set |
| **Microsoft GraphRAG** | Graph research/reference architecture | Studying community GraphRAG | Now largely maintenance-mode |
| **LightRAG** | Graph + vector RAG | Lightweight GraphRAG experimentation | Still requires graph-quality validation |
| **RAGFlow** | Integrated ingestion/RAG/agents | Document-heavy self-hosted RAG experimentation | Larger integrated platform |
| **Ragas** | Evaluation | Regression and RAG quality measurement | Automated judges need calibration |

**LangChain/LangGraph.** Current LangChain documentation distinguishes predictable two-step RAG from agentic patterns; LangGraph exposes custom control over steps such as retrieval, document grading, query rewriting, and generation. LangSmith adds tracing/evaluation around those workflows. citeturn3search3turn3search6turn3search9

Use LangGraph when you genuinely need:

```text
retrieve
   ↓
is evidence sufficient?
   ├── yes → generate
   └── no
        ↓
rewrite/decompose
        ↓
retrieve again
        ↓
grade evidence
        ↓
generate/abstain
```

Do not introduce that loop merely because “agents” are fashionable.

**LlamaIndex.** LlamaIndex remains particularly strong for data-centric RAG. Its framework supports BM25 retrieval, custom retrievers, document indexing, and property-graph indexing. Its PropertyGraphIndex allows multiple node/path retrievers to be combined and supports richer graph structures than simple subject-predicate-object triples. citeturn5search1turn5search0turn5search3

It is a particularly good environment for experimentally comparing:

```text
vector retrieval
BM25
hybrid retrieval
document+chunk retrieval
graph retrieval
router-based retrieval
```

without rewriting the complete application for each experiment.

**Haystack.** Haystack's component-oriented architecture is attractive when you want explicit, inspectable pipelines. It provides retrievers, cross-encoder rankers, diversity rankers, and late-interaction rerankers, and its asynchronous pipelines can run independent retrieval branches concurrently. citeturn3search4turn3search13turn3search10turn3search25

That makes Haystack particularly well suited to architectures such as:

```text
                 ┌── BM25 ───────┐
query ───────────┤               ├── fusion → rerank → LLM
                 └── vector ─────┘
```

where the mechanics of each stage should remain obvious.

**DSPy.** DSPy treats LLM application behavior more like a program that can be optimized against examples and metrics rather than a manually maintained chain of prompts. Its official material includes basic RAG, evaluation, optimizers, and multi-hop retrieval/agents. citeturn3search2turn3search8turn3search20

DSPy becomes especially interesting once you already possess a high-quality evaluation dataset. You can optimize behavior against that dataset rather than repeatedly hand-editing prompts.

### Graph-focused packages

**Microsoft GraphRAG** remains an important reference implementation because it formalized the entity → relationship → community → community-summary architecture for broad corpus reasoning. However, Microsoft currently labels the GitHub project as largely in **maintenance mode**, with bug fixes/dependency updates rather than significant new feature development. That is an important 2026 consideration for new projects. citeturn6search0turn6search28

I would study Microsoft GraphRAG deeply for its architecture and benchmark against it, but I would not automatically make it the foundation of a new long-lived product without considering its maintenance status.

**LightRAG** takes a lighter graph/vector approach and describes itself as combining knowledge graphs with vector embeddings. Its project continues to receive active releases; recent releases add a PostgreSQL-native graph backend capable of consolidating multiple LightRAG storage roles into PostgreSQL. citeturn6search1turn6search13

This is one of the more interesting current GraphRAG projects to prototype.

**RAGFlow** is more integrated: it combines RAG functionality with agent capabilities and is oriented toward providing a fuller document-to-RAG workflow rather than just being a retriever library. citeturn6search2turn6search26

That makes RAGFlow attractive for rapid self-hosted experiments, particularly where document ingestion itself is a major part of the problem.

### Storage technology shortlist

For a serious proof-of-concept program, I would not test twenty databases. I would test one representative of each architectural family:

```text
Relational + vectors
    PostgreSQL + pgvector

Search-oriented hybrid
    Elasticsearch or OpenSearch

Vector-native
    Qdrant or Milvus
    optionally Weaviate/Pinecone depending hosted preference

Knowledge graph
    Neo4j
    or Neptune for AWS-centric infrastructure
```

That experiment tells you far more than comparing six vector databases that all implement variants of ANN retrieval.

## What large enterprises are actually doing and the architecture I would build

Public Fortune 100 disclosures are incomplete: firms generally do **not** publish complete production schemas, database topology, ranking models, security configuration, or proprietary evaluation sets. Therefore, claims that “Company X uses exactly this RAG stack” should be treated skeptically unless they come directly from the company.

There are nevertheless useful public signals.

Amazon is No. 1 on the 2026 Fortune 500. citeturn13search4turn13search26 Its current Bedrock Knowledge Bases architecture is instructive because the managed platform does **not** prescribe one primitive search method: it handles document ingestion, embeddings, vector stores, reranking, parsing and retrieval, supports multiple enterprise data sources, and now provides agentic retrieval intended for complex multi-step questions. It can also act as a retrieval provider for frameworks including LangChain and LlamaIndex. citeturn1search0

That is broadly the direction I would expect mature enterprise RAG to move:

```text
multiple authoritative sources
        ↓
managed ingestion / permissions
        ↓
multiple retrieval mechanisms
        ↓
routing + reranking
        ↓
traceable evidence
        ↓
agents only when required
```

J.P. Morgan Payments provides an even more revealing accuracy case study. In its public description of a client-facing AI virtual assistant, J.P. Morgan says it used RAG and then connected development to a test suite with tracing and LLM-as-a-judge measurements for correctness, completeness, and hallucination. The team reports that iterative prompt refinement, retrieval tuning, and expansion of its vector stores improved output correctness. citeturn2search0

The lesson is more important than the particular technology:

> **Large-enterprise RAG quality comes from an evaluation-and-improvement loop, not from installing a vector database.**

Capital One's current research points in the same direction. DF-RAG specifically investigates retrieval diversity rather than just increasing similarity, while another 2025 study compared multiple fine-tuning strategies and concluded that different training strategies could achieve similar quality improvements despite substantially different computational requirements. citeturn2search2turn2search5

### The architecture I would recommend for maximum accuracy

For an enterprise project starting today, I would implement the following.

```text
                        SOURCE OF TRUTH
 ┌───────────────────────────────────────────────────────────┐
 │ PostgreSQL / warehouse / APIs / object store / documents │
 └────────────────────────────┬──────────────────────────────┘
                              │
                   Versioned ingestion pipeline
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
       Documents         Structured rows      Relationships
          │                   │                   │
     parse/chunk          retain schema       entity linking
          │                   │                   │
          ▼                   ▼                   ▼
    Search index           SQL database        graph index
  BM25 + vectors                               + source refs
          │                   │                   │
          └───────────────────┼───────────────────┘
                              │
                         Query router
                              │
       ┌──────────────────────┼──────────────────────┐
       │                      │                      │
   hybrid search         validated SQL           graph
       │                      │                  traversal
       └──────────────────────┼──────────────────────┘
                              │
                       candidate fusion
                              │
                           reranker
                              │
                     diversity / dedupe
                              │
                       evidence verifier
                              │
                             LLM
                              │
                     claim verification
                              │
                  citations / abstention
```

The **canonical source layer** should retain immutable source identifiers and versions. Vector indexes and knowledge graphs should be rebuildable derived products.

The **document layer** should start with hybrid BM25+dense retrieval rather than vectors alone, a recommendation strongly supported by current retrieval research and by the convergence of major search products around hybrid retrieval. citeturn12academia34turn7search1turn7search2turn8search0

The **precision layer** should retrieve broadly and rerank narrowly. That is preferable to putting hundreds of chunks into the LLM and hoping its attention mechanism finds the correct one. Current search products and Haystack explicitly support this two-stage design. citeturn7search6turn3search10

The **structured layer** should generate SQL only when the question is genuinely structured and should validate SQL through schema restrictions, read-only credentials, execution, result sanity checks, and—where feasible—comparison with known equivalent queries. Spider 2.0 demonstrates that real enterprise text-to-SQL remains much harder than older benchmark results imply. citeturn11search1

The **graph layer** should be optional. Add it after you demonstrate that an important fraction of real queries require relationships or multi-hop reasoning. Current research supports significant graph advantages for those tasks but does not support the proposition that GraphRAG universally improves ordinary QA. citeturn10search25turn0search4

The **evaluation layer** should be independent of the framework used to build RAG. That protects you from unconsciously choosing metrics that favor your preferred architecture.

### Technologies and approaches I would prioritize

My high-priority investigation order would be:

**Hybrid BM25 + dense retrieval → reranking → excellent parsing/chunking → metadata filtering → exact provenance → evaluation harness → query routing → structured SQL retrieval → diversity-aware retrieval → conditional multi-query/decomposition → GraphRAG where justified → agentic retrieval for genuinely complex questions.**

That ordering is consistent with current empirical results showing particularly strong returns from hybrid retrieval and reranking and less consistent benefits from query-expansion techniques. citeturn12academia34

For code, I would shortlist **LlamaIndex, LangChain/LangGraph, Haystack, DSPy, LightRAG, RAGFlow, and Ragas**, with Microsoft GraphRAG retained as a reference/benchmark implementation rather than automatically selected as a new production foundation because of its current maintenance-mode status. citeturn5search3turn3search6turn3search25turn3search2turn6search1turn6search2turn10search3turn6search0

For storage, I would benchmark **PostgreSQL/pgvector, Elasticsearch or OpenSearch, Qdrant or Milvus, and Neo4j** against the *same corpus and same query set*. That spans relational+vector, lexical+vector search, vector-native retrieval, and graph retrieval without exploding the proof-of-concept matrix.

### Technologies and practices I would not make the default

I would **not start with vector-only RAG** unless benchmarks show lexical retrieval adds no value. Current research and product design both argue strongly for hybrid retrieval. citeturn12academia34turn8search3

I would **not automatically build GraphRAG for an FAQ or ordinary documentation bot**. GraphRAG's benefits become much clearer for relational/multi-hop tasks and substantially less consistent for ordinary factual QA. citeturn10search25turn0search4

I would **not treat LLM-extracted graph edges as facts**. Keep source-level provenance and schema validation so every important graph result can be traced back to original evidence. SHACL and graph-database constraints provide deterministic structural checks, but factual provenance must still come from the underlying source. citeturn9search0turn9search1

I would **not interpret cosine similarity as confidence**. Similarity, relevance, answerability, factuality, and source authority are separate concepts; diversity-focused retrieval research demonstrates that simply retrieving the most similar chunks can produce redundant and inferior evidence sets. citeturn2search2

I would **not accept ANN accuracy on faith**. Periodically compare approximate retrieval against exact nearest neighbors and establish a minimum Recall@K requirement. pgvector makes the distinction between exact and approximate search particularly explicit. citeturn7search0

I would **not select a vector database from vendor QPS charts alone**. ANN-Benchmarks, VectorDBBench and RAGPerf all reinforce the importance of comparing speed and quality systematically, and RAGPerf additionally emphasizes whole-pipeline measurements rather than isolated vector-search throughput. citeturn12search1turn12search0turn12search5

I would **not send every structured database question through embeddings**. For counts, sums, joins, exact date ranges and deterministic business predicates, validated SQL preserves semantics that embeddings discard. The difficulty of enterprise text-to-SQL means this path still requires significant validation, but the underlying database remains the correct computational engine. citeturn11search1turn11search3

I would **not put every retrieved chunk into the context window**. Retrieval quality includes elimination of irrelevant and redundant evidence, not just recall; both modern reranking systems and recent diversity research support retrieving broadly and then selecting a smaller, higher-quality evidence set. citeturn7search6turn2search2

### The most useful research papers and resources to read next

| Research/resource | Why it matters |
|---|---|
| **From BM25 to Corrective RAG: Benchmarking Retrieval Strategies for Text-and-Table Documents** | Excellent recent empirical comparison of BM25, dense, hybrid, reranking, query expansion and adaptive methods; especially useful because it finds hybrid+reranking strongest and BM25 surprisingly competitive. citeturn12academia34 |
| **RAG vs. GraphRAG: A Systematic Evaluation** | Direct comparison of ordinary RAG and graph approaches rather than assuming GraphRAG superiority. citeturn0search1turn0search15 |
| **When to Use Graphs in RAG / GraphRAG-Bench** | Explicitly investigates when graphs help and when they do not. citeturn10search7 |
| **Do We Still Need GraphRAG?** | Strong recent evidence distinguishing ordinary QA from multi-hop use cases. citeturn10search25 |
| **The Reasoning Bottleneck in Graph-RAG** | Important demonstration that successful retrieval and successful reasoning are different problems. citeturn10academia31 |
| **DF-RAG** | Enterprise research into relevance/diversity tradeoffs in retrieval. citeturn2search2 |
| **Spider 2.0** | One of the strongest warnings against overestimating production text-to-SQL reliability. citeturn11search1 |
| **BIRD** | Useful structured-data benchmark emphasizing executable and efficient SQL. citeturn11search3 |
| **RAGPerf** | Measures both RAG system performance and accuracy across components. citeturn12search5 |
| **Microsoft GraphRAG** | Important reference implementation and architecture, while noting its 2026 maintenance-mode status. citeturn6search0turn6search28 |
| **LightRAG** | Active lightweight graph/vector RAG project worth benchmarking against Microsoft's architecture. citeturn6search1turn6search13 |
| **LlamaIndex Property Graph / retrieval framework** | Practical environment for comparing vector, BM25 and graph-based retrieval. citeturn5search0turn5search17 |
| **LangGraph Agentic RAG** | Useful implementation reference for conditional retrieval, grading and rewriting. citeturn3search6 |
| **Haystack Rankers and pipelines** | Good implementation references for explicit retrieve-then-rerank architectures. citeturn3search19turn3search25 |
| **DSPy RAG and multi-hop tutorials** | Useful if you want optimization/evaluation to drive RAG behavior rather than manually tuning prompts indefinitely. citeturn3search2turn3search20 |
| **Ragas** | Practical application-level RAG evaluation and test-set generation. citeturn10search3 |
| **SHACL** | Essential resource when RDF knowledge-graph correctness and schema validation matter. citeturn9search0turn9search4 |

The larger conclusion from this research is that **RAG accuracy in 2026 is becoming a systems-engineering problem rather than an embedding problem**. The strongest architectures combine deterministic databases for deterministic facts, hybrid search for documents, reranking for precision, graphs where relationships genuinely matter, provenance to recover original evidence, and a continuously growing evaluation set to determine whether each additional component actually helps. Current research repeatedly shows that more retrieval, more graph structure, more context, or more agent steps do **not** automatically produce a better answer. citeturn12academia34turn10search25turn10academia31

The best criterion for every proposed addition is consequently:

> **On our own frozen test corpus, does this component measurably improve retrieval recall, final factual accuracy, citation correctness, and/or latency-cost efficiency without creating a larger failure surface?**

That question—not whether the technology is called vector search, GraphRAG, agentic RAG, NoSQL, or knowledge graphs—is the foundation of a high-accuracy production RAG system.
