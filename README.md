# Event-driven Invoice Intelligence Pipeline

Flow: upload a PDF to the bucket, which fires a finalize event to a Pub/Sub topic. A push to the worker extracts
the fields, validates them with Pydantic plus business rules (GSTIN checksum, line maths, tax, totals), then
writes clean rows to the warehouse. Anything uncertain or rule-breaking goes to a human review queue with the
reason. Delivery is at-least-once with retries and a dead-letter queue, and processing is idempotent (keyed by
SHA-256).

```bash
python -m fde4_doc_pipeline.generate_invoices   # 120 labelled invoices (3 layouts) + 10 from an unseen layout
python -m fde4_doc_pipeline.evaluate            # field accuracy, routing precision/recall, idempotency
uvicorn fde4_doc_pipeline.api:app --port 8004   # POST /upload, GET /review, POST /pubsub/push
```

## Quick start

```bash
python -m venv .venv && .venv\Scripts\activate    # Windows; source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
python -m fde4_doc_pipeline.generate_invoices
python -m fde4_doc_pipeline.evaluate
uvicorn fde4_doc_pipeline.api:app --port 8080
pytest -q
```

Everything runs **offline**: no API keys or cloud account are needed. The steps that would use an LLM have
deterministic offline implementations, so tests are reproducible and answers cannot be hallucinated.

## Results (synthetic, seeded data)

- Field-level accuracy on 120 labelled invoices: **100.0%** (synthetic, text-based PDFs from known layouts).
- Planted errors (bad totals, bad GSTIN check digits) caught: recall 100.0%, precision 100.0%; 90.0% processed with no human review.
- Unseen supplier layout: 0/10 auto-approved (all routed to review).
- Re-delivering 30 files created no duplicate rows; a poison message was dead-lettered after 5 attempts.
- 2.55 ms per document.

## Switching to the real services

| Environment variable | Effect |
|---|---|
| `GEMINI_API_KEY` (+ `GEMINI_MODEL`) | use Gemini for the LLM steps |
| `GOOGLE_CLOUD_PROJECT` + `FDE_USE_VERTEX=1` | Vertex AI text-embedding-005 instead of the offline hashing embedder |
| `FDE_VECTOR_BACKEND=chroma` or `pgvector` (+ `PGVECTOR_DSN`) | Chroma or PostgreSQL/pgvector instead of the NumPy vector store |

The cloud adapters are written against the official SDKs but have not been run against live services yet.
Deploy to Cloud Run with the included `Dockerfile` (`gcloud run deploy --source .`).

## Layout

- `fde4_doc_pipeline/`: the project (see the module docstrings; each file explains its role)
- `common/`: shared tokeniser + BM25, hashing embedder / Vertex embedder, vector stores (NumPy, Chroma, pgvector), Gemini client
- `docs/FDE_Projects_Guide.pdf`: a detailed write-up of this project and its five siblings

## Part of a series of six Forward Deployed Engineer projects

- [Agentic RAG Assistant on Google ADK](https://github.com/SritarunLakkaraju/agentic-rag-adk)
- [DealMind v2: Sales Agent on LangGraph](https://github.com/SritarunLakkaraju/dealmind-langgraph)
- [CrewAI Weekly Business Report on a BigQuery-style Warehouse](https://github.com/SritarunLakkaraju/crewai-bigquery-reports)
- [Vector Search from Scratch: HNSW vs FAISS vs Chroma](https://github.com/SritarunLakkaraju/hnsw-vector-search)
- [Customer Support Agent for a Small Business (LangGraph)](https://github.com/SritarunLakkaraju/support-agent-langgraph)
