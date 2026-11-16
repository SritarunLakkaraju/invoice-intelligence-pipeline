r"""Wires the event-driven pipeline together and runs it over a folder of PDFs.

    upload -> LocalBucket (GCS) --finalize--> PubSub topic --push--> Worker (Cloud Run) -> SqlStore (BigQuery)
                                                                                     \-> review queue
"""
from __future__ import annotations

import os

from .infra import LocalBucket, PubSub, SqlStore
from .worker import Worker

HERE = os.path.dirname(os.path.abspath(__file__))


class Pipeline:
    def __init__(self, root: str, db: str = ":memory:", extractor=None):
        self.bucket = LocalBucket(os.path.join(root, "bucket"))
        self.bus = PubSub()
        self.store = SqlStore(db)
        self.worker = Worker(self.bucket, self.store, extractor)
        self.bucket.on_finalize(lambda ev: self.bus.publish(ev))  # bucket notification -> topic

    def upload(self, path: str) -> dict:
        return self.bucket.upload(path)

    def run(self) -> int:
        return self.bus.drain(self.worker.handle_push)


def run_folder(src: str, root: str, db: str = ":memory:") -> Pipeline:
    p = Pipeline(root, db)
    for fn in sorted(os.listdir(src)):
        if fn.lower().endswith(".pdf"):
            p.upload(os.path.join(src, fn))
    p.run()
    return p


if __name__ == "__main__":
    import tempfile

    from .generate_invoices import OUT, generate

    if not os.path.isdir(OUT) or not os.listdir(OUT):
        generate()
    with tempfile.TemporaryDirectory() as tmp:
        p = run_folder(OUT, tmp)
        print(f"auto-approved: {p.store.count('invoices')}, sent to review: {p.store.count('review_queue')}, "
              f"dead-lettered: {len(p.bus.dead_letter)}")
