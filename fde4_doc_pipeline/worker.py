"""The function Cloud Run runs for each Pub/Sub push: decode event -> fetch PDF -> extract -> validate -> route.

Idempotent: the document id is the SHA-256 of the file, so a redelivered message (Pub/Sub is at-least-once)
or the same PDF uploaded twice never creates a second row.
"""
from __future__ import annotations

import base64
import json
import time

from .extract import OfflineInvoiceExtractor, pdf_text
from .infra import LocalBucket, SqlStore, sha256_file
from .validate import validate


class Worker:
    def __init__(self, bucket: LocalBucket, store: SqlStore, extractor=None):
        self.bucket, self.store = bucket, store
        self.extractor = extractor or OfflineInvoiceExtractor()
        self.results: list[dict] = []

    def handle_push(self, envelope: dict) -> dict:
        event = json.loads(base64.b64decode(envelope["message"]["data"]))
        return self.process(event["name"])

    def process(self, object_name: str) -> dict:
        t0 = time.perf_counter()
        path = self.bucket.path(object_name)
        doc_id = sha256_file(path)
        if self.store.seen(doc_id):
            return {"object_name": object_name, "doc_id": doc_id, "route": "duplicate", "reasons": []}
        fields, conf = self.extractor.extract(pdf_text(path))
        inv, reasons = validate(fields, conf)
        if inv is not None and not reasons:
            self.store.insert_invoice(doc_id, object_name, inv.model_dump())
            route = "auto"
        else:
            self.store.enqueue_review(doc_id, object_name, fields, reasons)
            route = "review"
        res = {"object_name": object_name, "doc_id": doc_id, "route": route, "reasons": reasons, "fields": fields,
               "confidence": conf, "ms": round((time.perf_counter() - t0) * 1000, 2)}
        self.results.append(res)
        print(json.dumps({"severity": "INFO", "event": "processed", "object": object_name, "route": route,
                          "reasons": reasons[:3], "ms": res["ms"]}))  # structured log line for Cloud Logging
        return res
