"""Local stand-ins for the GCP pieces, with the same semantics that matter:

  LocalBucket   ~ Cloud Storage bucket: upload() stores the object and fires an OBJECT_FINALIZE notification.
  PubSub        ~ Pub/Sub topic + push subscription: at-least-once delivery, retries with backoff counted per
                  message, a dead-letter topic after max_attempts, message ids. (Handlers must be idempotent.)
  SqlStore      ~ BigQuery tables `invoices` and `review_queue` (SQLite file locally). BigQueryStore has the
                  same methods on the real service.

In production: GCS bucket notification -> Pub/Sub topic -> push subscription -> Cloud Run service
POST /pubsub/push (see api.py), which accepts the real Pub/Sub push envelope.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable


class LocalBucket:
    def __init__(self, root: str, name: str = "invoices-inbox"):
        self.root, self.name = root, name
        os.makedirs(root, exist_ok=True)
        self.listeners: list[Callable[[dict], None]] = []

    def on_finalize(self, fn: Callable[[dict], None]):
        self.listeners.append(fn)

    def path(self, object_name: str) -> str:
        return os.path.join(self.root, object_name)

    def upload(self, local_path: str, object_name: str | None = None) -> dict:
        object_name = object_name or os.path.basename(local_path)
        shutil.copyfile(local_path, self.path(object_name))
        event = {"bucket": self.name, "name": object_name, "size": os.path.getsize(self.path(object_name))}
        for fn in self.listeners:
            fn(event)
        return event


@dataclass
class Message:
    id: str
    data: bytes
    attempts: int = 0
    errors: list[str] = field(default_factory=list)


class PubSub:
    def __init__(self, max_attempts: int = 5):
        self.max_attempts = max_attempts
        self.queue: list[Message] = []
        self.dead_letter: list[Message] = []
        self.delivered = 0
        self.lock = threading.Lock()

    def publish(self, payload: dict) -> str:
        m = Message(id=uuid.uuid4().hex[:12], data=json.dumps(payload).encode())
        with self.lock:
            self.queue.append(m)
        return m.id

    @staticmethod
    def push_envelope(m: Message) -> dict:
        """The JSON body Pub/Sub POSTs to a push endpoint."""
        return {"message": {"data": base64.b64encode(m.data).decode(), "messageId": m.id},
                "subscription": "projects/local/subscriptions/invoices-push"}

    def drain(self, handler: Callable[[dict], None]) -> int:
        """Deliver until the queue is empty. A handler exception = nack: redelivered until max_attempts,
        then moved to the dead-letter list."""
        n = 0
        while True:
            with self.lock:
                if not self.queue:
                    return n
                m = self.queue.pop(0)
            m.attempts += 1
            try:
                handler(self.push_envelope(m))
                self.delivered += 1
                n += 1
            except Exception as e:  # noqa: BLE001 - any failure is a nack
                m.errors.append(f"{type(e).__name__}: {e}")
                with self.lock:
                    (self.queue if m.attempts < self.max_attempts else self.dead_letter).append(m)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class SqlStore:
    def __init__(self, path: str = ":memory:"):
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS invoices (doc_id TEXT PRIMARY KEY, object_name TEXT, invoice_number TEXT,
                invoice_date TEXT, vendor_name TEXT, vendor_gstin TEXT, buyer_gstin TEXT, subtotal REAL,
                tax REAL, total REAL, line_items TEXT, source TEXT, processed_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS review_queue (doc_id TEXT PRIMARY KEY, object_name TEXT, fields TEXT,
                reasons TEXT, status TEXT DEFAULT 'open', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)

    def seen(self, doc_id: str) -> bool:
        with self.lock:
            q = "SELECT 1 FROM invoices WHERE doc_id=? UNION SELECT 1 FROM review_queue WHERE doc_id=?"
            return self.con.execute(q, (doc_id, doc_id)).fetchone() is not None

    def insert_invoice(self, doc_id: str, object_name: str, inv: dict, source: str = "auto"):
        with self.lock, self.con:
            self.con.execute("INSERT OR IGNORE INTO invoices (doc_id, object_name, invoice_number, invoice_date, "
                             "vendor_name, vendor_gstin, buyer_gstin, subtotal, tax, total, line_items, source) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (doc_id, object_name, inv["invoice_number"], str(inv["invoice_date"]), inv["vendor_name"],
                              inv["vendor_gstin"], inv["buyer_gstin"], inv["subtotal"],
                              round(inv["cgst"] + inv["sgst"] + inv["igst"], 2), inv["total"],
                              json.dumps(inv["line_items"], default=str), source))

    def enqueue_review(self, doc_id: str, object_name: str, fields: dict, reasons: list[str]):
        with self.lock, self.con:
            self.con.execute("INSERT OR IGNORE INTO review_queue (doc_id, object_name, fields, reasons) VALUES (?,?,?,?)",
                             (doc_id, object_name, json.dumps(fields, default=str), json.dumps(reasons)))

    def open_reviews(self) -> list[dict]:
        with self.lock:
            rows = self.con.execute("SELECT doc_id, object_name, fields, reasons FROM review_queue "
                                    "WHERE status='open' ORDER BY created_at").fetchall()
        return [{"doc_id": r[0], "object_name": r[1], "fields": json.loads(r[2]), "reasons": json.loads(r[3])}
                for r in rows]

    def resolve_review(self, doc_id: str, status: str):
        with self.lock, self.con:
            self.con.execute("UPDATE review_queue SET status=? WHERE doc_id=?", (status, doc_id))

    def count(self, table: str) -> int:
        with self.lock:
            return self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def invoices(self) -> list[dict]:
        with self.lock:
            cur = self.con.execute("SELECT * FROM invoices ORDER BY invoice_date")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


class BigQueryStore(SqlStore):
    """Writes approved invoices to BigQuery with the streaming API. Not exercised by the offline tests."""

    def __init__(self, table: str):
        super().__init__(":memory:")  # review queue stays transactional (in production: Firestore)
        from google.cloud import bigquery

        self.client, self.table = bigquery.Client(), table

    def insert_invoice(self, doc_id, object_name, inv, source="auto"):
        row = {"doc_id": doc_id, "object_name": object_name, **{k: str(v) if k == "invoice_date" else v
                                                               for k, v in inv.items() if k != "line_items"},
               "line_items": json.dumps(inv["line_items"], default=str), "source": source}
        errors = self.client.insert_rows_json(self.table, [row], row_ids=[doc_id])  # row_ids = dedupe key
        if errors:
            raise RuntimeError(errors)
