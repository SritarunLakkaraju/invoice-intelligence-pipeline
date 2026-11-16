# FDE-4 · Event-driven Invoice Intelligence Pipeline

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
